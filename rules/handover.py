"""handover rule.

Activation lives in two places:

  1. The agent-callable ``trigger_handover`` tool (see
     ``tools/trigger_handover.py``) — primary path, used when the
     conversational agent itself decides handover is needed.
  2. **Implicit** activation when the owner types in the customer chat
     directly (only available when the WhatsApp adapter forwards
     ``metadata['whatsapp_from_owner']`` — the
     ``WHATSAPP_FORWARD_OWNER_MESSAGES`` env flag in hermes-agent).
     Treat any owner-typed inbound as proof the owner is engaged: if no
     handover is active yet, activate one (``activated_by="owner_implicit"``,
     no Telegram notify because the owner is already in the chat); if one
     is already active, just slide the TTL forward.  This keeps the
     handover alive for the duration of the owner's manual conversation.

Beyond activation, the rule still handles:

  * ``exit_command`` (e.g. ``/takeback``) → deactivate + notify.  This is
    checked **before** the owner-implicit branch so an owner sending
    ``/takeback`` ends the handover even though the same message also
    carries the ``whatsapp_from_owner`` flag.
  * Customer message during an active handover → silent ingest, no reply.

Gateway-side activation by phrase match / LLM classifier was removed —
phrases were unreliable across languages and the classifier duplicated
the main agent's own context-aware judgement.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..notify import format_chat_link, notify_owner
from ..state import alias_chat_ids
from ..transcript_utils import (
    HANDOVER_ENDED_NOTE,
    append_boundary_note,
    silent_ingest,
)

logger = logging.getLogger("gateway-policy.rules.handover")


def _platform_str(source: Any) -> str:
    plat = getattr(source, "platform", None)
    if plat is None:
        return ""
    return getattr(plat, "value", str(plat)).lower()


def _is_owner_message(event: Any, owner_platform: Optional[str], owner_chat_id: Optional[str]) -> bool:
    # Bridge-LRU-authenticated owner: any inbound the WhatsApp adapter
    # tagged with whatsapp_from_owner came from the bot's own account
    # (i.e. the human owner typing on the same number) and is owner-
    # equivalent regardless of how `handover.owner.*` is configured.
    # This unblocks /takeback in profiles where owner.platform points
    # at a separate notification channel (e.g. Telegram) but the human
    # actually replies in WhatsApp.
    metadata = getattr(event, "metadata", None) or {}
    if metadata.get("whatsapp_from_owner"):
        return True
    if not owner_platform or not owner_chat_id:
        return False
    source = event.source
    if _platform_str(source) != owner_platform:
        return False
    sender = (
        getattr(source, "user_id", None)
        or getattr(source, "chat_id", None)
        or ""
    )
    return str(sender) == owner_chat_id


def _deactivate(state, gateway, session_store, *, platform: str, chat_id: str, source) -> None:
    cfg = state.config.handover
    row = state.handovers.deactivate(platform, chat_id)
    if not row:
        return
    # Boundary marker so the agent's next reply doesn't parrot stale
    # "owner has been notified" turns from earlier in this session.
    append_boundary_note(
        session_store,
        source,
        text=HANDOVER_ENDED_NOTE,
        kind="handover_ended",
    )
    if cfg.owner.platform and cfg.owner.chat_id:
        customer_name = (
            getattr(source, "user_name", None)
            or getattr(source, "user_id", None)
            or "customer"
        )
        customer_phone, customer_link = format_chat_link(platform, chat_id)
        message = cfg.notify_on_exit.format(
            customer_name=customer_name,
            chat_id=chat_id,
            platform=platform,
            reason=row.reason,
            activated_by=row.activated_by or "",
            customer_phone=customer_phone,
            customer_link=customer_link,
        )
        notify_owner(
            gateway,
            owner_platform=cfg.owner.platform,
            owner_chat_id=cfg.owner.chat_id,
            message=message,
        )


def handover_rule(*, event, gateway, session_store, state, **_kwargs) -> Optional[Dict[str, Any]]:
    cfg = state.config.handover
    if not cfg.enabled:
        return None

    source = event.source
    platform = _platform_str(source)
    chat_id = str(getattr(source, "chat_id", "") or "")

    if cfg.platforms and platform not in cfg.platforms:
        return None
    if not chat_id:
        return None

    text = (event.text or "").strip()

    # Alias-aware lookup: WhatsApp chats can surface as either a phone JID
    # (``60123456789@s.whatsapp.net``) or a LID (``999999999999@lid``)
    # for the same human, depending on protocol negotiation. Probing only
    # the inbound form would silently miss a row stored under the other
    # variant — which is how the bot stayed live for Kong while the
    # handover row keyed by his phone JID was active. ``alias_chat_ids``
    # enumerates every form for the lookup, then we pin all subsequent
    # writes to the *stored* chat_id so we don't fragment the row across
    # variants.
    candidates = alias_chat_ids(platform, chat_id)
    # Drop expired rows for this chat first so the boundary-note path
    # below can detect a "just expired" transition without racing
    # find_active's own lazy delete.
    expired_rows = state.handovers.expire_stale(platform, candidates)
    active_row = state.handovers.find_active(platform, candidates)
    is_active = active_row is not None
    stored_chat_id = active_row.chat_id if active_row else chat_id

    # Boundary marker only on a genuine ON->OFF transition.  A WhatsApp
    # chat can carry two alias-keyed rows (phone-JID vs LID); if one
    # expires while the other is still active the bot must stay silent
    # AND we must not hint to the agent that handover ended.
    if expired_rows and not is_active:
        append_boundary_note(
            session_store,
            source,
            text=HANDOVER_ENDED_NOTE,
            kind="handover_expired",
        )

    # 1) /takeback first — must beat the owner-implicit branch so an owner
    # sending the exit command ends the handover even though the same
    # inbound also carries the whatsapp_from_owner metadata flag.
    if (
        is_active
        and cfg.exit_command
        and text.lower() == cfg.exit_command.lower()
        and _is_owner_message(event, cfg.owner.platform, cfg.owner.chat_id)
    ):
        _deactivate(
            state,
            gateway,
            session_store,
            platform=platform,
            chat_id=stored_chat_id,
            source=source,
        )
        return {"action": "skip", "reason": "handover_exit"}

    # 2) Owner-implicit activation / TTL slide.  Driven entirely by adapter
    # metadata — see WhatsAppAdapter._build_message_event in hermes-agent.
    # No notify on either branch: the owner is already in the chat, so a
    # Telegram ping would be noise.
    metadata = getattr(event, "metadata", None) or {}
    if metadata.get("whatsapp_from_owner"):
        ttl = cfg.timeout_minutes * 60 if cfg.timeout_minutes else None
        if is_active:
            if ttl:
                state.handovers.touch(platform, stored_chat_id, ttl)
            silent_ingest(session_store, event, reason="handover_owner_extend")
            return {"action": "skip", "reason": "handover_owner_extend"}

        # Cold chat -> activate without notify.  ``activated_by`` doubles as
        # the suppress-notify signal in trigger_handover.py / future call
        # sites; ``reason`` documents *why* in the persistent row.
        state.handovers.activate(
            platform,
            chat_id,
            reason="owner_reply",
            activated_by="owner_implicit",
            ttl_seconds=ttl,
        )
        silent_ingest(session_store, event, reason="handover_owner_activate")
        return {"action": "skip", "reason": "handover_owner_activate"}

    # 3) Existing path: customer-side messages during an active handover get
    # silently ingested so the owner can catch up later.
    if not is_active:
        return None

    silent_ingest(session_store, event, reason="handover_active")
    return {"action": "skip", "reason": "handover_active"}

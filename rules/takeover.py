"""takeover rule (owner silence-bot / bot silent-ingest).

Activation lives in two places:

  1. The agent-callable ``trigger_takeover`` tool (see
     ``tools/trigger_takeover.py``) — primary path, used when the
     conversational agent itself decides takeover is needed.
  2. **Implicit** activation when the owner types in the customer chat
     directly (only available when the WhatsApp adapter forwards
     ``metadata['whatsapp_from_owner']`` — the
     ``WHATSAPP_FORWARD_OWNER_MESSAGES`` env flag in hermes-agent).
     Treat any owner-typed inbound as proof the owner is engaged: if no
     takeover is active yet, activate one (``activated_by="owner_implicit"``,
     no Telegram notify because the owner is already in the chat); if one
     is already active, just slide the TTL forward.

Beyond activation, the rule still handles:

  * ``exit_command`` (e.g. ``/handover``) → deactivate + notify. Checked
    **before** the owner-implicit branch so an owner sending ``/handover``
    resumes the bot even though the same message may also carry the
    ``whatsapp_from_owner`` flag.
  * Customer message during an active takeover → silent ingest, no reply.

Gateway-side activation by phrase match / LLM classifier was removed.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..notify import format_chat_link, format_notify_on_exit, notify_owner
from ..state import alias_chat_ids
from ..transcript_utils import (
    TAKEOVER_ENDED_NOTE,
    append_boundary_note,
    silent_ingest,
)

logger = logging.getLogger("gateway-policy.rules.takeover")


def _platform_str(source: Any) -> str:
    plat = getattr(source, "platform", None)
    if plat is None:
        return ""
    return getattr(plat, "value", str(plat)).lower()


def _is_owner_message(event: Any, owner_platform: Optional[str], owner_chat_id: Optional[str]) -> bool:
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


def _deactivate_takeover(
    state,
    gateway,
    session_store,
    *,
    platform: str,
    chat_id: str,
    source,
    notify_owner_on_exit: bool = True,
) -> None:
    cfg = state.config.handover
    row = state.takeovers.deactivate(platform, chat_id)
    if not row:
        return
    append_boundary_note(
        session_store,
        source,
        text=TAKEOVER_ENDED_NOTE,
        kind="takeover_ended",
    )
    if (
        notify_owner_on_exit
        and cfg.owner.platform
        and cfg.owner.chat_id
    ):
        customer_name = (
            getattr(source, "user_name", None)
            or getattr(source, "user_id", None)
            or "customer"
        )
        message = format_notify_on_exit(customer_name=str(customer_name))
        notify_owner(
            gateway,
            owner_platform=cfg.owner.platform,
            owner_chat_id=cfg.owner.chat_id,
            message=message,
        )


def takeover_rule(*, event, gateway, session_store, state, **_kwargs) -> Optional[dict[str, Any]]:
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

    candidates = alias_chat_ids(platform, chat_id)
    expired_rows = state.takeovers.expire_stale(platform, candidates)
    active_row = state.takeovers.find_active(platform, candidates)
    is_active = active_row is not None
    stored_chat_id = active_row.chat_id if active_row else chat_id

    if expired_rows and not is_active:
        append_boundary_note(
            session_store,
            source,
            text=TAKEOVER_ENDED_NOTE,
            kind="takeover_expired",
        )

    # 1) /handover (resume bot) first — beats owner-implicit so exit wins
    if (
        is_active
        and cfg.exit_command
        and text.lower() == cfg.exit_command.lower()
        and _is_owner_message(event, cfg.owner.platform, cfg.owner.chat_id)
    ):
        _deactivate_takeover(
            state,
            gateway,
            session_store,
            platform=platform,
            chat_id=stored_chat_id,
            source=source,
            notify_owner_on_exit=True,
        )
        return {"action": "skip", "reason": "takeover_exit"}

    metadata = getattr(event, "metadata", None) or {}
    if metadata.get("whatsapp_from_owner"):
        ttl = cfg.timeout_minutes * 60 if cfg.timeout_minutes else None
        if is_active:
            if ttl:
                state.takeovers.touch(platform, stored_chat_id, ttl)
            silent_ingest(session_store, event, reason="takeover_owner_extend")
            return {"action": "skip", "reason": "takeover_owner_extend"}

        state.takeovers.activate(
            platform,
            chat_id,
            reason="owner_reply",
            activated_by="owner_implicit",
            ttl_seconds=ttl,
        )
        silent_ingest(session_store, event, reason="takeover_owner_activate")
        return {"action": "skip", "reason": "takeover_owner_activate"}

    if not is_active:
        return None

    silent_ingest(session_store, event, reason="takeover_active")
    return {"action": "skip", "reason": "takeover_active"}

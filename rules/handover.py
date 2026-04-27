"""handover rule.

Activation lives entirely in the agent-callable ``trigger_handover`` tool
(see ``tools/trigger_handover.py``). This rule only enforces an *already
active* handover at the gateway:

  1. Customer message during an active handover -> silent ingest, no reply.
  2. Owner sends ``exit_command`` in the customer chat -> deactivate + notify.

Gateway-side activation (phrase match, LLM classifier) was removed —
phrases were unreliable across languages and the classifier duplicated
the main agent's own context-aware judgement.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..notify import notify_owner
from ..transcript_utils import silent_ingest

logger = logging.getLogger("gateway-policy.rules.handover")


def _platform_str(source: Any) -> str:
    plat = getattr(source, "platform", None)
    if plat is None:
        return ""
    return getattr(plat, "value", str(plat)).lower()


def _is_owner_message(event: Any, owner_platform: Optional[str], owner_chat_id: Optional[str]) -> bool:
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


def _deactivate(state, gateway, *, platform: str, chat_id: str, source) -> None:
    cfg = state.config.handover
    row = state.handovers.deactivate(platform, chat_id)
    if not row:
        return
    if cfg.owner.platform and cfg.owner.chat_id:
        customer_name = (
            getattr(source, "user_name", None)
            or getattr(source, "user_id", None)
            or "customer"
        )
        message = cfg.notify_on_exit.format(
            customer_name=customer_name,
            chat_id=chat_id,
            platform=platform,
            reason=row.reason,
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

    if not state.handovers.is_active(platform, chat_id):
        return None

    text = (event.text or "").strip()

    # Owner steps into the customer chat with the exit command -> end handover.
    # Possible only if the owner is somehow present in the chat (common in
    # groups, less so in DMs without a shim).
    if (
        cfg.exit_command
        and text.lower() == cfg.exit_command.lower()
        and _is_owner_message(event, cfg.owner.platform, cfg.owner.chat_id)
    ):
        _deactivate(state, gateway, platform=platform, chat_id=chat_id, source=source)
        return {"action": "skip", "reason": "handover_exit"}

    silent_ingest(session_store, event, reason="handover_active")
    return {"action": "skip", "reason": "handover_active"}

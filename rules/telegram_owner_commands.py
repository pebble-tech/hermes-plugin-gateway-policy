"""Owner-only Telegram slash commands: ``/handover_<chat>`` / ``/takeback_<chat>``."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, Optional

from ..notify import format_chat_link, notify_owner
from ..state import alias_chat_ids
from ..tg_commands import parse_owner_command
from .handover import _deactivate, _platform_str

logger = logging.getLogger("gateway-policy.rules.telegram_owner_commands")


def _customer_transcript_source(
    platform: str,
    chat_id: str,
    *,
    user_name: Optional[str] = None,
) -> Any:
    plat = SimpleNamespace(value=platform.lower())
    return SimpleNamespace(
        platform=plat,
        chat_id=chat_id,
        chat_type="dm",
        user_id=chat_id,
        user_name=user_name,
    )


def _customer_display_label(platform: str, chat_id: str, user_name: Optional[str]) -> str:
    if user_name:
        return user_name
    phone, _ = format_chat_link(platform, chat_id)
    return phone or chat_id


def _primary_handover_platform(cfg) -> str:
    platforms = cfg.platforms or ["whatsapp"]
    return str(platforms[0]).lower()


def telegram_owner_commands_rule(
    *, event, gateway, session_store, state, **_kwargs
) -> Optional[Dict[str, Any]]:
    cfg = state.config.handover
    if not cfg.enabled:
        return None

    source = event.source
    if _platform_str(source) != "telegram":
        return None
    if (cfg.owner.platform or "").lower() != "telegram" or not cfg.owner.chat_id:
        return None

    inbound_chat = str(getattr(source, "chat_id", "") or "").strip()
    if inbound_chat != str(cfg.owner.chat_id).strip():
        return None

    parsed = parse_owner_command(event.text or "")
    if not parsed:
        return None

    action, target_chat_id = parsed
    customer_platform = _primary_handover_platform(cfg)
    if cfg.platforms and customer_platform not in cfg.platforms:
        logger.warning(
            "telegram owner command ignored: primary platform %r not in handover.platforms",
            customer_platform,
        )
        return None

    candidates = alias_chat_ids(customer_platform, target_chat_id)
    state.handovers.expire_stale(customer_platform, candidates)
    active_row = state.handovers.find_active(customer_platform, candidates)
    stored_chat_id = active_row.chat_id if active_row else target_chat_id

    if action == "takeback":
        if not active_row:
            warn = f"⚠ No active handover for {target_chat_id}."
            notify_owner(
                gateway,
                owner_platform=cfg.owner.platform,
                owner_chat_id=cfg.owner.chat_id,
                message=warn,
            )
            return {"action": "skip", "reason": "owner_telegram_command"}

        label = _customer_display_label(
            customer_platform,
            stored_chat_id,
            user_name=None,
        )
        t_source = _customer_transcript_source(
            customer_platform,
            stored_chat_id,
            user_name=None,
        )
        _deactivate(
            state,
            gateway,
            session_store,
            platform=customer_platform,
            chat_id=stored_chat_id,
            source=t_source,
            notify_owner_on_exit=False,
        )
        ok = (
            f"✓ Takeback complete: {label}. Bot is responding again."
        )
        notify_owner(
            gateway,
            owner_platform=cfg.owner.platform,
            owner_chat_id=cfg.owner.chat_id,
            message=ok,
        )
        return {"action": "skip", "reason": "owner_telegram_command"}

    # handover
    ttl = cfg.timeout_minutes * 60 if cfg.timeout_minutes else None
    if active_row:
        if ttl:
            state.handovers.touch(customer_platform, stored_chat_id, ttl)
    else:
        state.handovers.activate(
            customer_platform,
            stored_chat_id,
            reason="manual handover from Telegram",
            activated_by="owner_telegram_command",
            ttl_seconds=ttl,
        )

    notify_owner(
        gateway,
        owner_platform=cfg.owner.platform,
        owner_chat_id=cfg.owner.chat_id,
        message=f"✓ Handover active: {stored_chat_id}. Bot silenced.",
    )
    return {"action": "skip", "reason": "owner_telegram_command"}

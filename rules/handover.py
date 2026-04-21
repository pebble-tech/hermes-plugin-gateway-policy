"""handover rule.

Three trigger tiers (in order of precedence per turn):
  1. Already-active handover     -> silent ingest, no reply.
  2. Exit command from owner     -> deactivate handover + notify.
  3. Phrase match in customer DM -> activate + ingest + notify.
  4. Optional aux-LLM classifier -> activate + ingest + notify.

Tier 5 (agent-driven `trigger_handover` tool) lives in
``tools/trigger_handover.py`` and runs at agent turn-time, not here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..notify import notify_owner
from ..transcript_utils import silent_ingest
from ..triggers import llm_classifier_says_handover, matches_phrases

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


def _activate(state, gateway, *, source, reason: str, activated_by: str = "") -> Dict[str, Any]:
    cfg = state.config.handover
    platform = _platform_str(source)
    chat_id = str(getattr(source, "chat_id", "") or "")
    if not platform or not chat_id:
        return {"action": "skip", "reason": "handover_invalid_target"}

    ttl = cfg.timeout_minutes * 60 if cfg.timeout_minutes else None
    row = state.handovers.activate(
        platform,
        chat_id,
        reason=reason,
        activated_by=activated_by,
        ttl_seconds=ttl,
    )
    customer_name = (
        getattr(source, "user_name", None) or getattr(source, "user_id", None) or "customer"
    )

    if cfg.owner.platform and cfg.owner.chat_id:
        message = cfg.notify_on_activate.format(
            customer_name=customer_name,
            chat_id=chat_id,
            platform=platform,
            reason=reason,
            activated_by=activated_by or "system",
        )
        ok = notify_owner(
            gateway,
            owner_platform=cfg.owner.platform,
            owner_chat_id=cfg.owner.chat_id,
            message=message,
        )
        if ok:
            state.handovers.mark_notified(platform, chat_id)
    logger.info(
        "handover activated: platform=%s chat=%s reason=%s by=%s",
        platform, chat_id, reason, activated_by,
    )
    return {"action": "skip", "reason": f"handover_activated:{reason}"}


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

    text = (event.text or "").strip()

    # ---------- Tier 0: owner exit command ----------
    if (
        cfg.exit_command
        and text.lower() == cfg.exit_command.lower()
        and _is_owner_message(event, cfg.owner.platform, cfg.owner.chat_id)
    ):
        # Owner sends `/takeback` to themselves to end the handover for the
        # most recently activated chat? More useful: owner sends it as a
        # reply / forward in the customer's chat. We support both:
        # if owner is in a customer chat, deactivate that one; otherwise
        # this rule is a no-op (owner DM is not a handover).
        return None  # owner DM exit-command requires more context; skip here.

    # ---------- Tier 1: handover already active ----------
    if state.handovers.is_active(platform, chat_id):
        # If the *owner* steps into the customer chat with the exit command,
        # that ends the handover. (Possible only if owner is somehow in the
        # chat — common in groups, less so in DMs without a shim.)
        if (
            cfg.exit_command
            and text.lower() == cfg.exit_command.lower()
            and _is_owner_message(event, cfg.owner.platform, cfg.owner.chat_id)
        ):
            _deactivate(state, gateway, platform=platform, chat_id=chat_id, source=source)
            return {"action": "skip", "reason": "handover_exit"}
        # Customer message during active handover → silent ingest.
        silent_ingest(session_store, event, reason="handover_active")
        return {"action": "skip", "reason": "handover_active"}

    # ---------- Tier 2: phrase match ----------
    triggers = cfg.triggers
    if triggers.phrases:
        matched = matches_phrases(text, triggers.phrases)
        if matched:
            silent_ingest(session_store, event, reason="handover_phrase_trigger")
            return _activate(
                state,
                gateway,
                source=source,
                reason=f"phrase:{matched}",
                activated_by="phrase_trigger",
            )

    # ---------- Tier 3: optional aux-LLM classifier ----------
    classifier = triggers.llm_classifier
    if classifier.enabled and text:
        if classifier.dm_only and getattr(source, "chat_type", "") != "dm":
            return None
        verdict = llm_classifier_says_handover(classifier.prompt, text)
        if verdict is True:
            silent_ingest(session_store, event, reason="handover_llm_trigger")
            return _activate(
                state,
                gateway,
                source=source,
                reason="llm_classifier",
                activated_by="llm_classifier",
            )

    return None

"""trigger_handover tool — agent-driven escalation to a human owner.

The agent calls this tool when it determines the conversation needs a human
(e.g., out-of-scope request, customer explicitly asked). Activates a
handover row for the *current* chat (looked up via task_id == session_key)
and notifies the owner via the existing gateway adapter.

The tool description is intentionally generic so the same plugin can be
used across profiles. Profile-specific "what counts as out of scope" rules
should live in the user's AGENTS.md / personality prompt — the tool
description tells the LLM to consult those operating instructions.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from ..notify import notify_owner

logger = logging.getLogger("gateway-policy.tools.trigger_handover")


_TOOL_DESCRIPTION = (
    "Hand the current conversation over to a human owner. The bot will stop "
    "replying to this customer; their messages are still recorded so the "
    "owner can catch up and respond manually.\n\n"
    "Call this tool ONLY when:\n"
    "  - The customer explicitly asks to speak to a human, the owner, or "
    "another person, OR\n"
    "  - The customer's request is clearly outside what you can handle "
    "according to your operating instructions (consult your system prompt "
    "/ AGENTS notes for the specific scope of your business).\n\n"
    "Do NOT call this tool for normal product questions, availability "
    "checks, simple orders, or anything you can reasonably fulfill yourself. "
    "Do NOT call it just because the customer is frustrated — only when "
    "human intervention is actually required.\n\n"
    "After calling this tool, send the customer a short friendly message "
    "letting them know the owner will follow up shortly, then end your turn."
)


_SCHEMA: Dict[str, Any] = {
    "name": "trigger_handover",
    "description": _TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "One short sentence (<= 200 chars) explaining why "
                    "handover is needed. Shown to the owner verbatim. "
                    "Example: 'Customer asked for custom design '"
                    "'consultation, which is outside our self-serve flow.'"
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "Optional 1-3 sentence summary of the conversation so "
                    "far, so the owner can quickly catch up."
                ),
            },
        },
        "required": ["reason"],
    },
}


def _ok(data: Dict[str, Any]) -> str:
    return json.dumps({"ok": True, **data})


def _err(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error_code": code, "error": message})


def _resolve_session_context(
    state: Any, task_id: Optional[str]
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[Any]]:
    """Recover (platform, chat_id, user_name, gateway) for a tool call.

    Tries the active_sessions stash first (set by the pre_gateway_dispatch
    hook); falls back to parsing task_id (= session_key:
    ``agent:main:<platform>:<chat_type>:<chat_id>[:<thread_id>]``).
    """
    if not task_id:
        return None, None, None, None

    cached = state.active_sessions.get(task_id)
    if cached:
        return cached  # (platform, chat_id, user_name, gateway)

    # Fallback: parse session key.
    parts = task_id.split(":")
    # ['agent', 'main', '<platform>', '<chat_type>', '<chat_id>', ...]
    if len(parts) >= 5 and parts[0] == "agent":
        return parts[2], parts[4], None, None
    return None, None, None, None


def make_trigger_handover_tool(
    get_state: Callable[[], Any]
) -> Tuple[Dict[str, Any], Callable[..., str]]:
    """Factory returning (schema, handler) for the trigger_handover tool."""

    def handler(args: Dict[str, Any], **kwargs) -> str:
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return _err("missing_reason", "reason is required")
        if len(reason) > 500:
            reason = reason[:497] + "..."
        summary = str(args.get("summary") or "").strip()

        state = get_state()
        cfg = state.config.handover
        if not cfg.enabled:
            return _err(
                "handover_disabled",
                "Handover is not enabled in the gateway-policy config.",
            )

        task_id = kwargs.get("task_id")
        platform, chat_id, user_name, gateway = _resolve_session_context(state, task_id)
        if not platform or not chat_id:
            return _err(
                "no_active_chat",
                "Could not resolve the current chat context (task_id missing or unparseable).",
            )

        if cfg.platforms and platform not in cfg.platforms:
            return _err(
                "platform_not_configured",
                f"Handover is not enabled for platform '{platform}'.",
            )

        ttl = cfg.timeout_minutes * 60 if cfg.timeout_minutes else None
        state.handovers.activate(
            platform,
            chat_id,
            reason=f"agent_tool:{reason}",
            activated_by="trigger_handover_tool",
            ttl_seconds=ttl,
        )

        notified = False
        if gateway and cfg.owner.platform and cfg.owner.chat_id:
            customer_name = user_name or "customer"
            message = cfg.notify_on_activate.format(
                customer_name=customer_name,
                chat_id=chat_id,
                platform=platform,
                reason=reason,
                activated_by="agent",
            )
            if summary:
                message = f"{message}\n\nAgent summary: {summary}"
            notified = notify_owner(
                gateway,
                owner_platform=cfg.owner.platform,
                owner_chat_id=cfg.owner.chat_id,
                message=message,
            )
            if notified:
                state.handovers.mark_notified(platform, chat_id)

        logger.info(
            "trigger_handover activated by agent: platform=%s chat=%s reason=%s notified=%s",
            platform, chat_id, reason, notified,
        )

        return _ok({
            "platform": platform,
            "chat_id": chat_id,
            "owner_notified": notified,
            "expires_at": (
                time.time() + ttl if ttl else None
            ),
        })

    return _SCHEMA, handler

"""listen_only rule.

For configured chats: on bot mention (or within an active follow-up window
when ``require_mention=False``), open/refresh the 2-minute window and let
the message go through.

Two modes, controlled by ``buffer_ambient``:

  * ``buffer_ambient=True`` (default) — "listen and collapse": untagged
    messages are appended to an in-memory buffer and silently ingested
    into the transcript. When the bot is tagged, the buffer is collapsed
    into a single ``Recent chat context:`` block on the user turn.
  * ``buffer_ambient=False`` — "tag + follow-up window only": pre-tag and
    out-of-window messages are dropped completely. Inside the window
    (and with ``require_mention=False``) any message triggers a reply.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from ..transcript_utils import silent_ingest
from ..triggers import is_bot_mentioned

logger = logging.getLogger("gateway-policy.rules.listen_only")


def _platform_str(source: Any) -> str:
    plat = getattr(source, "platform", None)
    if plat is None:
        return ""
    return getattr(plat, "value", str(plat)).lower()


def _chat_key(source: Any) -> Optional[Tuple[str, str]]:
    platform = _platform_str(source)
    chat_id = getattr(source, "chat_id", None)
    if not platform or not chat_id:
        return None
    return (platform, str(chat_id))


def _format_buffer(buffer, header: str) -> str:
    if not buffer:
        return ""
    lines = [header.rstrip(":") + ":"]
    for sender, text, _ts in buffer:
        speaker = sender or "user"
        lines.append(f"- {speaker}: {text}")
    return "\n".join(lines)


def listen_only_rule(*, event, gateway, session_store, state, **_kwargs) -> Optional[Dict[str, Any]]:
    cfg = state.config.listen_only
    if not cfg.chats:
        return None

    source = event.source
    if getattr(source, "chat_type", "") == "dm":
        return None

    key = _chat_key(source)
    if key is None:
        return None

    configured = {(c.platform, c.chat_id) for c in cfg.chats}
    if key not in configured:
        return None

    text = (event.text or "").strip()
    if not text:
        return None

    now = time.time()
    expiry = state.listen_windows.get(key, 0.0)
    window_active = expiry > now

    mentioned = is_bot_mentioned(event, extra_patterns=cfg.mention_patterns)

    # Path A: bot mentioned OR follow-up window active (when require_mention=False)
    # → reply (with buffered context if any).
    if mentioned or (window_active and not cfg.require_mention):
        # Refresh the follow-up window.
        if cfg.window_seconds > 0:
            state.listen_windows[key] = now + cfg.window_seconds

        buffer = state.buffer_for(key)
        if not buffer:
            # No prior context to inject — let normal dispatch run unchanged.
            return {"action": "allow"}

        prefixed = _format_buffer(buffer, cfg.rewrite_header)
        # Clear so we don't re-inject on the next turn.
        buffer.clear()
        rewritten = f"{prefixed}\n\nNew message: {text}"
        logger.info(
            "listen_only collapsing %d buffered msg(s) into reply (%s/%s)",
            len(prefixed.splitlines()) - 1,
            key[0],
            key[1],
        )
        return {"action": "rewrite", "text": rewritten}

    if window_active and cfg.require_mention and not mentioned:
        # Window is open but require_mention is true: still need a tag.
        # Buffer this message for the next tagged turn unless the operator
        # opted out of buffering.
        if not cfg.buffer_ambient:
            return {"action": "skip", "reason": "listen_only_window_no_mention"}
        sender = getattr(source, "user_name", None) or getattr(source, "user_id", None) or "user"
        state.buffer_for(key).append((str(sender), text, now))
        silent_ingest(session_store, event, reason="listen_only_window")
        return {"action": "skip", "reason": "listen_only_window_no_mention"}

    # Path C: ambient (no mention, no window).
    # With buffer_ambient=False the message is dropped silently — the operator
    # explicitly does NOT want pre-tag messages pulled into the transcript.
    if not cfg.buffer_ambient:
        return {"action": "skip", "reason": "listen_only_no_tag"}
    sender = getattr(source, "user_name", None) or getattr(source, "user_id", None) or "user"
    state.buffer_for(key).append((str(sender), text, now))
    silent_ingest(session_store, event, reason="listen_only_ambient")
    return {"action": "skip", "reason": "listen_only_ambient"}

"""Transcript helpers.

Mirrors Hermes core's group-thread `[sender name]` prefix convention so
silently-ingested messages don't appear anonymous in shared threads.
Reference: gateway/run.py group-prefix logic and gateway/session.py
_is_shared_thread system-prompt note.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("gateway-policy.transcript")


def silent_ingest(session_store: Any, event: Any, *, reason: str) -> None:
    """Append event.text to the transcript for its session without replying.

    - Applies the `[sender name]` prefix for non-DM chats when the sender
      is known (matching core's convention so replies can attribute
      messages to the right speaker).
    - No-op for empty text.
    """
    source = event.source
    message_text = (event.text or "").strip()
    if not message_text:
        return

    is_shared_thread = (
        getattr(source, "chat_type", None) != "dm"
        and getattr(source, "thread_id", None)
    )
    if is_shared_thread and getattr(source, "user_name", None):
        message_text = f"[{source.user_name}] {message_text}"

    try:
        session_entry = session_store.get_or_create_session(source)
    except Exception as exc:
        logger.warning("get_or_create_session failed: %s", exc)
        return

    ts = datetime.now().isoformat()
    try:
        session_store.append_to_transcript(
            session_entry.session_id,
            {"role": "user", "content": message_text, "timestamp": ts},
        )
        session_store.update_session(session_entry.session_key)
    except Exception as exc:
        logger.warning("silent_ingest append failed: %s", exc)
        return

    platform_val = (
        source.platform.value if hasattr(source.platform, "value") else str(source.platform)
    )
    logger.info(
        "silent ingest: reason=%s platform=%s chat=%s session=%s chars=%d",
        reason,
        platform_val,
        getattr(source, "chat_id", "unknown") or "unknown",
        session_entry.session_id,
        len(message_text),
    )

"""Transcript helpers.

Mirrors Hermes core's group-thread `[sender name]` prefix convention so
silently-ingested messages don't appear anonymous in shared threads.
Reference: gateway/run.py group-prefix logic and gateway/session.py
_is_shared_thread system-prompt note.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("gateway-policy.transcript")

_OWNER_REPLY_PREFIX = "[owner reply] "

# Boundary-note text used when a takeover ends (owner hands the chat back
# to the bot). The bracketed prefix is what AGENTS.md teaches the agent
# to recognise. Legacy sessions may still contain ``[handover-ended]``;
# treat both as equivalent until those transcripts rotate out.
TAKEOVER_ENDED_NOTE = (
    "[takeover-ended] The previous takeover for this chat has ended. "
    "Resume normal assistant behaviour. Do not reference earlier turns "
    "about the owner being notified — those messages are stale; treat "
    "the chat as live again from the next customer message."
)


def silent_ingest(session_store: Any, event: Any, *, reason: str) -> None:
    """Append event.text to the transcript for its session without replying.

    - Applies the `[sender name]` prefix for non-DM chats when the sender
      is known (matching core's convention so replies can attribute
      messages to the right speaker).
    - Owner-typed inbounds (carrying ``metadata['whatsapp_from_owner']``)
      get an explicit ``[owner reply]`` prefix when not already present on
      ``event.text`` (Hermes core may prefix at ``MessageEvent``
      construction; this path stays idempotent).  Without any prefix,
      the transcript stores both customer and owner-typed messages as
      plain ``role: "user"`` entries — so the agent's next reply naturally
      attributes everything to the customer (e.g. "Thanks Kong, I see
      your images" when Kong never sent any).  The bracketed prefix is
      what AGENTS.md teaches the agent to recognise as the owner
      speaking directly to the customer.
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

    metadata = getattr(event, "metadata", None) or {}
    if metadata.get("whatsapp_from_owner") and not message_text.startswith(
        _OWNER_REPLY_PREFIX
    ):
        message_text = f"{_OWNER_REPLY_PREFIX}{message_text}"

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


def append_boundary_note(
    session_store: Any,
    source: Any,
    *,
    text: str,
    kind: str,
) -> Optional[str]:
    """Write a one-off boundary marker into the chat's transcript.

    Used to mark transitions the agent would otherwise infer wrongly from
    its own past turns — e.g. a takeover ending mid-session leaves the
    transcript full of "owner has been notified" assistant turns that bias
    the next reply. A clearly-tagged ``role=user`` note (``[takeover-ended]
    ...``) gives the model an explicit "ignore prior takeover-era talk" cue
    on the next replay.

    ``role: "user"`` rather than ``"system"`` because mid-stream system
    messages aren't portable across adapters (Codex Responses drops them
    entirely; Anthropic / Gemini / Bedrock fold them back into the system
    block, losing position). A bracketed user note is universally honored
    and obviously not customer speech.

    Returns the session_id the note was written to, or ``None`` on failure.
    Failures are logged at warning level — boundary notes are advisory,
    not load-bearing for correctness.
    """
    if not text:
        return None
    try:
        session_entry = session_store.get_or_create_session(source)
    except Exception as exc:
        logger.warning("boundary note get_or_create_session failed: %s", exc)
        return None

    ts = datetime.now().isoformat()
    try:
        session_store.append_to_transcript(
            session_entry.session_id,
            {"role": "user", "content": text, "timestamp": ts},
        )
        session_store.update_session(session_entry.session_key)
    except Exception as exc:
        logger.warning("boundary note append failed: %s", exc)
        return None

    platform_val = (
        source.platform.value if hasattr(source.platform, "value") else str(source.platform)
    )
    logger.info(
        "boundary note: kind=%s platform=%s chat=%s session=%s chars=%d",
        kind,
        platform_val,
        getattr(source, "chat_id", "unknown") or "unknown",
        session_entry.session_id,
        len(text),
    )
    return session_entry.session_id

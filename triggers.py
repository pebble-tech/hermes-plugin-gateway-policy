"""Trigger detection helpers.

Currently only bot-mention detection (used by ``rules/listen_only.py``).
Phrase-matching and the optional LLM classifier paths were removed when
takeover activation was consolidated to the ``trigger_takeover`` tool —
they were unreliable across mixed-language deployments and redundant
with the main agent's own classification pass.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional, Pattern

logger = logging.getLogger("gateway-policy.triggers")

# Bot-mention patterns. We piggy-back on Hermes core's WhatsApp `@bot` /
# `@assistant` heuristic plus a generic explicit mention via raw_message
# fields when available.
_MENTION_TOKENS_RE = re.compile(
    r"(?<!\w)@(?:bot|assistant|hermes)(?!\w)", re.IGNORECASE
)


def is_bot_mentioned(
    event: Any,
    extra_patterns: Optional[Iterable[Pattern[str]]] = None,
) -> bool:
    """Heuristic: did this message tag the bot?

    True when:
      - text contains @bot / @assistant / @hermes (case-insensitive), OR
      - text matches one of ``extra_patterns`` (e.g. the adapter's
        ``whatsapp.mention_patterns`` like ``(?i)^esping\\b``), OR
      - raw_message hints at an explicit mention (WhatsApp ``mentioned``
        list contains the bot, Telegram entities of type ``mention``,
        Slack ``<@Uxxx>`` etc.). Best-effort; rules are conservative.
    """
    text = (event.text or "").strip()
    if text and _MENTION_TOKENS_RE.search(text):
        return True

    # Profile-configured mention patterns (e.g. "^esping\b" or "^bot\b").
    # Without this branch the plugin disagrees with the adapter and
    # silently ingests messages the adapter already treated as mentions.
    if text and extra_patterns:
        for pattern in extra_patterns:
            try:
                if pattern.search(text):
                    return True
            except Exception:  # noqa: BLE001 — defensive; never break dispatch
                continue

    raw = getattr(event, "raw_message", None)
    if isinstance(raw, dict):
        # WhatsApp bridge payload exposes both `mentionedIds` (every JID/LID
        # tagged in the message) and `botIds` (the bot's own identities). A
        # bot mention = the two sets intersect. Matching on any non-empty
        # `mentionedIds` alone would treat "@Alice look at this" as a tag
        # for us, which is wrong — especially once the adapter forwards
        # every group message via `free_response_chats`.
        mentioned_ids = raw.get("mentionedIds")
        bot_ids = raw.get("botIds")
        if isinstance(mentioned_ids, list) and isinstance(bot_ids, list) and bot_ids:
            bot_set = {_strip_id(b) for b in bot_ids if b}
            for mid in mentioned_ids:
                if not mid:
                    continue
                if _strip_id(mid) in bot_set:
                    return True

        # Explicit-flag fallback (some Telegram/Slack bridges).
        if raw.get("isMention") or raw.get("is_mention"):
            return True

    return False


def _strip_id(value: Any) -> str:
    """Reduce a WhatsApp JID/LID to the bare numeric identity."""
    return (
        str(value or "")
        .strip()
        .lstrip("+")
        .split(":", 1)[0]
        .split("@", 1)[0]
    )

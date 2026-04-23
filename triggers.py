"""Trigger detection: mentions, phrase matching, optional LLM classifier."""

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


def matches_phrases(text: str, phrases: list[str]) -> Optional[str]:
    """Return the first matched phrase (case-insensitive, substring), else None."""
    if not text or not phrases:
        return None
    needle = text.lower()
    for phrase in phrases:
        if not phrase:
            continue
        if phrase.lower() in needle:
            return phrase
    return None


def llm_classifier_says_handover(prompt: str, message: str) -> Optional[bool]:
    """Optional LLM classifier via Hermes auxiliary client.

    Returns True/False on a confident classification; None on any error
    (caller should treat None as "no decision").
    """
    text = (message or "").strip()
    if not text:
        return False
    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:
        logger.warning("auxiliary_client import failed: %s", exc)
        return None

    full_prompt = (
        f"{prompt}\n\n"
        f"Customer message:\n```\n{text}\n```\n\n"
        "Reply with exactly one word: 'yes' or 'no'."
    )
    try:
        result = call_llm(
            task="gateway_policy_classifier",
            prompt=full_prompt,
            max_tokens=4,
        )
    except Exception as exc:
        logger.warning("call_llm classifier failed: %s", exc)
        return None

    raw = ""
    if isinstance(result, str):
        raw = result
    elif isinstance(result, dict):
        raw = str(result.get("text") or result.get("content") or "")
    else:
        raw = str(result or "")
    raw = raw.strip().lower().strip(".!?\"' ")
    if raw.startswith("yes"):
        return True
    if raw.startswith("no"):
        return False
    logger.debug("classifier returned ambiguous response: %r", raw)
    return None

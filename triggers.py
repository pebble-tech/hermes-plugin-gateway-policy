"""Trigger detection: mentions, phrase matching, optional LLM classifier."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("gateway-policy.triggers")

# Bot-mention patterns. We piggy-back on Hermes core's WhatsApp `@bot` /
# `@assistant` heuristic plus a generic explicit mention via raw_message
# fields when available.
_MENTION_TOKENS_RE = re.compile(
    r"(?<!\w)@(?:bot|assistant|hermes)(?!\w)", re.IGNORECASE
)


def is_bot_mentioned(event: Any) -> bool:
    """Heuristic: did this message tag the bot?

    True when:
      - text contains @bot / @assistant / @hermes (case-insensitive), OR
      - raw_message hints at an explicit mention (WhatsApp ``mentioned``
        list contains the bot, Telegram entities of type ``mention``,
        Slack ``<@Uxxx>`` etc.). Best-effort; rules are conservative.
    """
    text = (event.text or "").strip()
    if text and _MENTION_TOKENS_RE.search(text):
        return True

    raw = getattr(event, "raw_message", None)
    if isinstance(raw, dict):
        # WhatsApp bridge sets `mentions` / `mentioned` lists when the
        # bot's number is tagged. We can't know the bot's number here,
        # so any non-empty mention list is treated as "tag" (acceptable
        # because non-tagged messages would never even be processed in
        # `require_mention=true` mode by the adapter).
        for key in ("mentioned", "mentions"):
            val = raw.get(key)
            if isinstance(val, list) and val:
                return True
        # Some adapters set this flag directly.
        if raw.get("isMention") or raw.get("is_mention"):
            return True

    return False


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

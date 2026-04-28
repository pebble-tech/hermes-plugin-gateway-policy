"""Telegram slash-command helpers for owner-side handover control.

Telegram only linkifies ``/commands`` when the command body matches
``[A-Za-z0-9_]`` (after the leading ``/``). WhatsApp JIDs contain ``@`` and
``.`` so we encode those for tappable owner commands and decode on receipt.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

_SUB_AT = "_AT_"
_SUB_DOT = "_DOT_"

# ``/verb@BotName_encodedtoken`` (groups): shortest bot handle (4–32 chars),
# then ``_``, then the encoded chat id.  Lazy quantifier so ``_AT_`` inside
# the token is not folded into the bot name.
_INLINE_BOT_RE = re.compile(
    r"^/(?P<verb>handover|takeback)@"
    r"(?P<bot>[A-Za-z][A-Za-z0-9_]{3,30}?)"
    r"_(?P<rest>\S+)$",
    re.IGNORECASE,
)
# Trailing ``@BotName`` on the whole command (groups / some clients).
_TRAILING_BOT_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,30}\s*$")

_CMD_RE = re.compile(
    r"^/(?P<verb>handover|takeback)_(?P<tok>\S+)$",
    re.IGNORECASE,
)


def encode_chat_id(chat_id: str) -> str:
    """Encode *chat_id* so the result is usable inside a tappable Telegram command.

    Substitutions: ``@`` → ``_AT_``, ``.`` → ``_DOT_``. Alphanumerics and
    underscore pass through. If anything else remains, raises *ValueError*
    so mis-encoded ids fail loudly instead of silently breaking commands.
    """
    if chat_id is None:
        raise ValueError("chat_id is required")
    s = str(chat_id).replace("@", _SUB_AT).replace(".", _SUB_DOT)
    if re.search(r"[^A-Za-z0-9_]", s):
        raise ValueError(
            f"chat_id cannot be encoded for Telegram commands (unsupported "
            f"characters after substitution): {chat_id!r} -> {s!r}"
        )
    return s


def decode_chat_id(token: str) -> str:
    """Reverse :func:`encode_chat_id`."""
    s = str(token).replace(_SUB_DOT, ".").replace(_SUB_AT, "@")
    return s


def parse_owner_command(text: str) -> Optional[Tuple[str, str]]:
    """Parse owner ``/handover_<token>`` or ``/takeback_<token>`` messages.

    Returns ``("handover", chat_id)`` or ``("takeback", chat_id)`` with
    *chat_id* decoded, or ``None`` if *text* is not a lone owner command.

    Strips a trailing ``@BotName`` suffix and normalizes
    ``/verb@BotName_token`` → ``/verb_token`` (Telegram group mention forms).
    """
    s = (text or "").strip()
    if not s:
        return None
    m_inline = _INLINE_BOT_RE.match(s)
    if m_inline:
        v = m_inline.group("verb").lower()
        s = f"/{v}_{m_inline.group('rest')}"
    s = _TRAILING_BOT_RE.sub("", s).strip()
    m = _CMD_RE.match(s)
    if not m:
        return None
    verb = m.group("verb").lower()
    tok = m.group("tok")
    return (verb, decode_chat_id(tok))

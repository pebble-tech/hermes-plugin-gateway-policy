"""Runtime state for gateway-policy: SQLite-backed handover state + in-memory
buffers and listen-only windows.

Handover rows must survive gateway restarts (handovers can be long-lived);
listen-only buffers/windows are ephemeral (2-minute windows, acceptable
to lose on restart).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from hermes_constants import get_hermes_home

from .config import PolicyConfig

logger = logging.getLogger("gateway-policy.state")


def whatsapp_alias_chat_ids(chat_id: str) -> List[str]:
    """Return all WhatsApp JID forms that alias the same chat.

    The bridge surfaces the same human under either the phone JID
    (``60123456789@s.whatsapp.net``) or the LID (``999999999999@lid``)
    depending on protocol version, contact-card state, and whatever
    Baileys learned in the current session. State keyed by one form
    silently misses lookups using the other, which is exactly the bug
    that allowed the bot to keep replying after a handover was active.

    We resolve the alias set via
    :func:`gateway.whatsapp_identity.expand_whatsapp_aliases` (the same
    helper Hermes core uses for session keys) and re-form each numeric
    alias as both ``@s.whatsapp.net`` and ``@lid`` so callers can probe
    every shape an old row could have been stored under.

    The original ``chat_id`` is always first in the result so callers
    that pick the first hit preserve previous semantics. Returns
    ``[chat_id]`` when the helper is unavailable (older Hermes) or no
    mapping files exist yet — in that case the caller is no worse off
    than before.
    """
    if not chat_id:
        return []
    try:
        from gateway.whatsapp_identity import expand_whatsapp_aliases
    except ImportError:
        return [chat_id]
    try:
        bare = expand_whatsapp_aliases(chat_id)
    except Exception as exc:
        logger.debug("expand_whatsapp_aliases(%r) failed: %s", chat_id, exc)
        return [chat_id]
    if not bare:
        return [chat_id]

    forms: List[str] = [chat_id]
    seen = {chat_id}
    # Sort numeric aliases so the canonical (shortest, lexicographically
    # smallest) form is probed before any longer LID — speeds up the
    # common case where only one alias is actually stored.
    for numeric in sorted(bare, key=lambda v: (len(v), v)):
        for variant in (numeric, f"{numeric}@s.whatsapp.net", f"{numeric}@lid"):
            if variant not in seen:
                seen.add(variant)
                forms.append(variant)
    return forms


def alias_chat_ids(platform: str, chat_id: str) -> List[str]:
    """Platform-aware alias expansion for handover lookups.

    WhatsApp chats can flip between phone-JID and LID forms; other
    platforms have stable chat ids and pass through unchanged.
    """
    if (platform or "").lower() == "whatsapp":
        return whatsapp_alias_chat_ids(chat_id)
    return [chat_id] if chat_id else []


@dataclass
class HandoverRow:
    platform: str
    chat_id: str
    reason: str
    activated_at: float
    activated_by: str
    expires_at: Optional[float]
    notified: bool

    @property
    def key(self) -> Tuple[str, str]:
        return (self.platform, self.chat_id)


def _state_dir() -> Path:
    # Profile-aware via get_hermes_home()
    path = get_hermes_home() / "workspace" / "state" / "gateway-policy"
    path.mkdir(parents=True, exist_ok=True)
    return path


class HandoverStore:
    """Thin SQLite wrapper for the handovers table."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS handovers (
        platform      TEXT NOT NULL,
        chat_id       TEXT NOT NULL,
        reason        TEXT,
        activated_at  REAL NOT NULL,
        activated_by  TEXT,
        expires_at    REAL,
        notified      INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (platform, chat_id)
    )
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,  # autocommit
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(self._SCHEMA)

    def activate(
        self,
        platform: str,
        chat_id: str,
        *,
        reason: str,
        activated_by: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> HandoverRow:
        now = time.time()
        expires_at = (now + ttl_seconds) if ttl_seconds and ttl_seconds > 0 else None
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO handovers
                    (platform, chat_id, reason, activated_at, activated_by,
                     expires_at, notified)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(platform, chat_id) DO UPDATE SET
                    reason=excluded.reason,
                    activated_at=excluded.activated_at,
                    activated_by=excluded.activated_by,
                    expires_at=excluded.expires_at
                """,
                (platform, chat_id, reason, now, activated_by, expires_at),
            )
        return HandoverRow(
            platform=platform,
            chat_id=chat_id,
            reason=reason,
            activated_at=now,
            activated_by=activated_by,
            expires_at=expires_at,
            notified=False,
        )

    def get(self, platform: str, chat_id: str) -> Optional[HandoverRow]:
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """SELECT platform, chat_id, reason, activated_at, activated_by,
                          expires_at, notified
                     FROM handovers WHERE platform=? AND chat_id=?""",
                (platform, chat_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        return HandoverRow(
            platform=row[0],
            chat_id=row[1],
            reason=row[2] or "",
            activated_at=row[3],
            activated_by=row[4] or "",
            expires_at=row[5],
            notified=bool(row[6]),
        )

    def is_active(self, platform: str, chat_id: str) -> bool:
        row = self.get(platform, chat_id)
        if not row:
            return False
        if row.expires_at is not None and row.expires_at < time.time():
            # lazy expiry
            self.deactivate(platform, chat_id)
            return False
        return True

    def find_active(
        self, platform: str, candidates: Iterable[str]
    ) -> Optional[HandoverRow]:
        """Return the first non-expired row for any of ``candidates``.

        Used by callers that know multiple chat_id forms can alias to the
        same chat (e.g. WhatsApp phone-JID vs LID). Expired rows are
        deactivated lazily, matching :meth:`is_active`'s contract.
        Returns ``None`` if no candidate has an active row.
        """
        seen: set = set()
        now = time.time()
        for cid in candidates:
            if not cid or cid in seen:
                continue
            seen.add(cid)
            row = self.get(platform, cid)
            if not row:
                continue
            if row.expires_at is not None and row.expires_at < now:
                self.deactivate(platform, cid)
                continue
            return row
        return None

    def touch(
        self,
        platform: str,
        chat_id: str,
        ttl_seconds: float,
    ) -> bool:
        """Slide the expiry on an existing handover.

        Updates ``expires_at = now + ttl_seconds`` only if a row already
        exists *and* it has a TTL set (a row with ``expires_at IS NULL``
        means "no auto-expiry" — we don't want to retroactively impose
        one just because the owner kept typing).  Returns True iff a row
        was updated; False on cold or no-TTL rows.

        Idempotent — safe to call from the pre-dispatch hook on every
        owner-typed inbound; a single SQL UPDATE keeps the cost trivial.
        """
        if ttl_seconds is None or ttl_seconds <= 0:
            return False
        new_expires = time.time() + ttl_seconds
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """
                UPDATE handovers
                   SET expires_at = ?
                 WHERE platform = ?
                   AND chat_id = ?
                   AND expires_at IS NOT NULL
                """,
                (new_expires, platform, chat_id),
            )
            return (cur.rowcount or 0) > 0

    def mark_notified(self, platform: str, chat_id: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE handovers SET notified=1 WHERE platform=? AND chat_id=?",
                (platform, chat_id),
            )

    def deactivate(self, platform: str, chat_id: str) -> Optional[HandoverRow]:
        row = self.get(platform, chat_id)
        if not row:
            return None
        with self._lock:
            conn = self._connect()
            conn.execute(
                "DELETE FROM handovers WHERE platform=? AND chat_id=?",
                (platform, chat_id),
            )
        return row

    def list_active(self) -> List[HandoverRow]:
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """SELECT platform, chat_id, reason, activated_at, activated_by,
                          expires_at, notified FROM handovers"""
            )
            rows = cur.fetchall()
        out: List[HandoverRow] = []
        now = time.time()
        for r in rows:
            expires_at = r[5]
            if expires_at is not None and expires_at < now:
                self.deactivate(r[0], r[1])
                continue
            out.append(
                HandoverRow(
                    platform=r[0],
                    chat_id=r[1],
                    reason=r[2] or "",
                    activated_at=r[3],
                    activated_by=r[4] or "",
                    expires_at=expires_at,
                    notified=bool(r[6]),
                )
            )
        return out


@dataclass
class PolicyState:
    """Container for config + handover store + ephemeral in-memory state."""

    config: PolicyConfig
    _store: Optional[HandoverStore] = None
    # (platform, chat_id) -> deque of (user_name, text, timestamp)
    buffers: Dict[Tuple[str, str], Deque[Tuple[str, str, float]]] = field(default_factory=dict)
    # (platform, chat_id) -> window expiry epoch
    listen_windows: Dict[Tuple[str, str], float] = field(default_factory=dict)
    # session_key -> most-recent (platform, chat_id, user_name, gateway_ref).
    # Populated by the pre_gateway_dispatch hook so the trigger_handover
    # tool (which only receives task_id == session_key) can recover its
    # acting context.
    active_sessions: Dict[str, Tuple[str, str, str, Any]] = field(default_factory=dict)

    @property
    def handovers(self) -> HandoverStore:
        if self._store is None:
            self._store = HandoverStore(_state_dir() / "state.db")
        return self._store

    def buffer_for(self, key: Tuple[str, str]) -> Deque[Tuple[str, str, float]]:
        buf = self.buffers.get(key)
        if buf is None:
            buf = deque(maxlen=self.config.listen_only.buffer_max)
            self.buffers[key] = buf
        return buf

"""Tests for gateway-policy rules and the trigger_handover tool."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

# Local FakeSource/FakeEvent are plain classes (not fixtures) so tests that
# need bespoke sources (e.g. telegram) can construct them inline. The
# conftest-level fixtures wrap these for the common DM/group cases.

@dataclass
class FakeSource:
    platform_str: str = "whatsapp"
    user_id: str = "15551234567@s.whatsapp.net"
    chat_id: str = "120363xyz@g.us"
    user_name: str = "Customer"
    chat_type: str = "group"
    thread_id: Optional[str] = None
    platform: Any = None

    def __post_init__(self):
        self.platform = SimpleNamespace(value=self.platform_str)


@dataclass
class FakeEvent:
    text: str
    source: Any
    raw_message: dict = field(default_factory=dict)
    message_id: str = "m1"
    internal: bool = False
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# listen_only
# ---------------------------------------------------------------------------

class TestListenOnly:
    def _add_chat(self, fresh_state, src):
        from gateway_policy.config import ChatRef
        fresh_state.config.listen_only.chats = [
            ChatRef(platform=src.platform.value, chat_id=src.chat_id)
        ]

    def test_ambient_message_buffered_and_skipped(self, fresh_state, src_group, session_store, gateway):
        from gateway_policy.rules.listen_only import listen_only_rule
        self._add_chat(fresh_state, src_group)

        event = FakeEvent(text="hi everyone", source=src_group)
        result = listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "listen_only_ambient"}
        assert len(fresh_state.buffer_for(("whatsapp", src_group.chat_id))) == 1
        assert len(session_store.appended) == 1  # silently ingested

    def test_mention_collapses_buffer_into_rewrite(self, fresh_state, src_group, session_store, gateway):
        from gateway_policy.rules.listen_only import listen_only_rule
        self._add_chat(fresh_state, src_group)
        # Pre-buffer two ambient messages.
        fresh_state.buffer_for(("whatsapp", src_group.chat_id)).extend(
            [("Alice", "wants 100", time.time()),
             ("Bob", "blue color", time.time())]
        )

        event = FakeEvent(text="@bot give us the quote", source=src_group)
        result = listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result["action"] == "rewrite"
        assert "Alice" in result["text"] and "Bob" in result["text"]
        assert "@bot give us the quote" in result["text"]
        # Buffer cleared after collapse.
        assert len(fresh_state.buffer_for(("whatsapp", src_group.chat_id))) == 0
        # Window opened.
        assert fresh_state.listen_windows[("whatsapp", src_group.chat_id)] > time.time()

    def test_adapter_mention_pattern_triggers_reply(
        self, fresh_state, src_group, session_store, gateway
    ):
        """Regression: profile-level `whatsapp.mention_patterns` like
        ``(?i)^esping\\b`` should be treated as mentions by the plugin.
        Previously only @bot/@assistant/@hermes literals worked, so
        messages the adapter forwarded as mentions were silently ingested."""
        import re
        from gateway_policy.rules.listen_only import listen_only_rule

        self._add_chat(fresh_state, src_group)
        fresh_state.config.listen_only.mention_patterns = [
            re.compile(r"(?i)^esping\b"),
            re.compile(r"(?i)^bot\b"),
        ]

        event = FakeEvent(text="esping check stock please", source=src_group)
        result = listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        # No buffered context, so "allow" (normal dispatch) is the right
        # answer — the critical part is that it is NOT a `skip`.
        assert result == {"action": "allow"}, (
            "adapter mention_patterns must be honored by listen_only_rule"
        )
        # Window should be open for follow-up replies.
        assert fresh_state.listen_windows[("whatsapp", src_group.chat_id)] > time.time()

    def test_chat_not_in_config_passes_through(self, fresh_state, src_group, session_store, gateway):
        from gateway_policy.rules.listen_only import listen_only_rule
        # No chats configured.
        event = FakeEvent(text="hi", source=src_group)
        assert listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        ) is None

    def test_dm_passes_through(self, fresh_state, src_dm, session_store, gateway):
        from gateway_policy.rules.listen_only import listen_only_rule
        self._add_chat(fresh_state, src_dm)
        event = FakeEvent(text="hi", source=src_dm)
        assert listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        ) is None

    def test_window_active_with_require_mention_still_buffers(
        self, fresh_state, src_group, session_store, gateway
    ):
        from gateway_policy.rules.listen_only import listen_only_rule
        self._add_chat(fresh_state, src_group)
        fresh_state.listen_windows[("whatsapp", src_group.chat_id)] = time.time() + 30
        event = FakeEvent(text="more context", source=src_group)
        result = listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        # require_mention=True (default in fresh_state) → still buffer, no reply.
        assert result["action"] == "skip"
        assert result["reason"] == "listen_only_window_no_mention"

    def test_buffer_ambient_false_drops_pretag_without_silent_ingest(
        self, fresh_state, src_group, session_store, gateway
    ):
        """With buffer_ambient=False, untagged messages outside the window
        must be skipped without any transcript write or buffer append."""
        from gateway_policy.rules.listen_only import listen_only_rule
        self._add_chat(fresh_state, src_group)
        fresh_state.config.listen_only.buffer_ambient = False

        event = FakeEvent(text="hi everyone", source=src_group)
        result = listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "listen_only_no_tag"}
        # No buffer append, no silent ingest.
        assert len(fresh_state.buffer_for(("whatsapp", src_group.chat_id))) == 0
        assert len(session_store.appended) == 0

    def test_non_bot_mention_does_not_trigger_reply(
        self, fresh_state, src_group, session_store, gateway
    ):
        """Regression: a group message tagging a non-bot user (e.g.
        `@Alice take a look`) sets `raw_message.mentionedIds = [alice_id]`.
        The plugin must NOT treat that as a bot mention. It may only trust
        the tag when `mentionedIds` intersects `botIds`."""
        from gateway_policy.rules.listen_only import listen_only_rule
        self._add_chat(fresh_state, src_group)

        event = FakeEvent(
            text="look at this",
            source=src_group,
            raw_message={
                "mentionedIds": ["60111111111@s.whatsapp.net"],
                "botIds": ["60999999999@s.whatsapp.net"],
            },
        )
        result = listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "listen_only_ambient"}
        assert ("whatsapp", src_group.chat_id) not in fresh_state.listen_windows

    def test_bot_mention_via_mentionedids_intersection(
        self, fresh_state, src_group, session_store, gateway
    ):
        """Complement: when mentionedIds *does* contain the bot, open the window."""
        from gateway_policy.rules.listen_only import listen_only_rule
        self._add_chat(fresh_state, src_group)

        event = FakeEvent(
            text="help",
            source=src_group,
            raw_message={
                "mentionedIds": ["60999999999@s.whatsapp.net"],
                "botIds": ["60999999999@s.whatsapp.net"],
            },
        )
        result = listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "allow"}
        assert fresh_state.listen_windows[("whatsapp", src_group.chat_id)] > time.time()

    def test_window_active_without_require_mention_replies_to_followups(
        self, fresh_state, src_group, session_store, gateway
    ):
        """Regression: the 2-min follow-up window must let untagged
        messages through as `allow` when require_mention=False."""
        from gateway_policy.rules.listen_only import listen_only_rule
        self._add_chat(fresh_state, src_group)
        fresh_state.config.listen_only.require_mention = False
        fresh_state.config.listen_only.buffer_ambient = False
        fresh_state.listen_windows[("whatsapp", src_group.chat_id)] = time.time() + 30

        event = FakeEvent(text="say want to do xxx", source=src_group)
        result = listen_only_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "allow"}
        # Window refreshed on the follow-up.
        assert fresh_state.listen_windows[("whatsapp", src_group.chat_id)] > time.time() + 29


# ---------------------------------------------------------------------------
# handover
# ---------------------------------------------------------------------------

class TestHandover:
    def test_disabled_returns_none(self, fresh_state, src_dm, session_store, gateway):
        from gateway_policy.rules.handover import handover_rule
        fresh_state.config.handover.enabled = False
        # Pre-activate to prove `enabled=False` short-circuits even when state exists.
        fresh_state.handovers.activate(
            "whatsapp", src_dm.chat_id, reason="manual", activated_by="test"
        )
        event = FakeEvent(text="hello", source=src_dm)
        assert handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        ) is None

    def test_inactive_chat_passes_through(self, fresh_state, src_dm, session_store, gateway):
        """No active handover -> rule is a no-op. Activation now happens
        only via the trigger_handover tool, never from inbound text."""
        from gateway_policy.rules.handover import handover_rule
        event = FakeEvent(text="please let me speak to a human now", source=src_dm)
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result is None
        assert not fresh_state.handovers.is_active("whatsapp", src_dm.chat_id)
        assert len(session_store.appended) == 0

    def test_active_handover_silent_ingests_subsequent_messages(
        self, fresh_state, src_dm, session_store, gateway
    ):
        from gateway_policy.rules.handover import handover_rule
        fresh_state.handovers.activate(
            "whatsapp", src_dm.chat_id, reason="manual", activated_by="test"
        )
        event = FakeEvent(text="just some follow-up", source=src_dm)
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "handover_active"}
        assert len(session_store.appended) == 1

    def test_other_platform_skipped(self, fresh_state, session_store, gateway):
        from gateway_policy.rules.handover import handover_rule
        tg = FakeSource(platform_str="telegram", chat_type="dm", chat_id="-100")
        event = FakeEvent(text="hi", source=tg)
        # Handover platforms = ['whatsapp'] only.
        assert handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        ) is None
        assert not fresh_state.handovers.is_active("telegram", "-100")

    def test_owner_exit_command_in_active_chat_deactivates(
        self, fresh_state, src_dm, session_store, gateway
    ):
        """Owner sending /takeback in the customer's chat ends the handover."""
        from gateway_policy.rules.handover import handover_rule
        fresh_state.handovers.activate(
            "whatsapp", src_dm.chat_id, reason="manual", activated_by="test"
        )
        # Spoof the source so the message looks like it came from the owner.
        owner_src = FakeSource(
            platform_str="whatsapp",
            chat_type="dm",
            chat_id=src_dm.chat_id,
            user_id=fresh_state.config.handover.owner.chat_id,
            user_name="Owner",
        )
        event = FakeEvent(text="/takeback", source=owner_src)
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "handover_exit"}
        assert not fresh_state.handovers.is_active("whatsapp", src_dm.chat_id)

    def test_takeback_via_whatsapp_from_owner_flag_alone_deactivates(
        self, fresh_state, src_dm, session_store, gateway
    ):
        """Profiles where ``handover.owner.platform`` points at the
        notification channel (e.g. Telegram) but the human owner actually
        types in WhatsApp must still be able to ``/takeback``. The bridge
        LRU classifier on the agent side stamps such inbounds with
        ``metadata['whatsapp_from_owner']=True`` — that flag alone is
        sufficient proof of ownership, no platform/sender match needed."""
        from gateway_policy.rules.handover import handover_rule

        # Owner configured for a *different* platform than the inbound —
        # the legacy (platform, sender_id) match would never fire here.
        fresh_state.config.handover.owner.platform = "telegram"
        fresh_state.config.handover.owner.chat_id = "640466638"

        fresh_state.handovers.activate(
            "whatsapp", src_dm.chat_id, reason="manual", activated_by="test"
        )
        # sender_id is some random WhatsApp JID, not the configured owner.
        non_owner_src = FakeSource(
            platform_str="whatsapp",
            chat_type="dm",
            chat_id=src_dm.chat_id,
            user_id="60173380115@s.whatsapp.net",
            user_name="Owner-on-WA",
        )
        event = FakeEvent(
            text="/takeback",
            source=non_owner_src,
            metadata={"whatsapp_from_owner": True},
        )
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "handover_exit"}
        assert not fresh_state.handovers.is_active("whatsapp", src_dm.chat_id)

    def test_takeback_without_owner_flag_from_stranger_does_not_deactivate(
        self, fresh_state, src_dm, session_store, gateway
    ):
        """Negative complement: a ``/takeback`` from a sender that is
        neither the configured owner nor flagged ``whatsapp_from_owner``
        must NOT end the handover. Pre-existing trust model is preserved."""
        from gateway_policy.rules.handover import handover_rule

        fresh_state.handovers.activate(
            "whatsapp", src_dm.chat_id, reason="manual", activated_by="test"
        )
        stranger_src = FakeSource(
            platform_str="whatsapp",
            chat_type="dm",
            chat_id=src_dm.chat_id,
            user_id="60199999999@s.whatsapp.net",
            user_name="Random",
        )
        event = FakeEvent(
            text="/takeback",
            source=stranger_src,
            metadata={"whatsapp_from_owner": False},
        )
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        # Falls through to the active-handover silent-ingest path, never
        # the exit branch — handover stays active.
        assert result == {"action": "skip", "reason": "handover_active"}
        assert fresh_state.handovers.is_active("whatsapp", src_dm.chat_id)

    def test_takeback_with_owner_flag_still_deactivates(
        self, fresh_state, src_dm, session_store, gateway
    ):
        """Order matters: a /takeback inbound that *also* carries the
        whatsapp_from_owner metadata flag (because both signals derive
        from the same fromMe message) must end the handover, not slide
        the TTL forward."""
        from gateway_policy.rules.handover import handover_rule
        fresh_state.handovers.activate(
            "whatsapp",
            src_dm.chat_id,
            reason="manual",
            activated_by="test",
            ttl_seconds=600,
        )
        owner_src = FakeSource(
            platform_str="whatsapp",
            chat_type="dm",
            chat_id=src_dm.chat_id,
            user_id=fresh_state.config.handover.owner.chat_id,
            user_name="Owner",
        )
        event = FakeEvent(
            text="/takeback",
            source=owner_src,
            metadata={"whatsapp_from_owner": True},
        )
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "handover_exit"}
        assert not fresh_state.handovers.is_active("whatsapp", src_dm.chat_id)


# ---------------------------------------------------------------------------
# WhatsApp phone-JID <-> LID alias keying
# ---------------------------------------------------------------------------

class TestWhatsappAliasKeying:
    """Regression for the live bug where Kong's inbound surfaced as
    ``122299244130458@lid`` while the active handover row was keyed by
    ``60173380115@s.whatsapp.net``. Both forms identify the same human;
    the bridge can flip between them. The rule must look up handover
    state through every alias the bridge knows about, otherwise the bot
    silently bypasses an active handover and replies to the customer.
    """

    @staticmethod
    def _stub_aliases(monkeypatch, mapping):
        """Stub :func:`gateway.whatsapp_identity.expand_whatsapp_aliases`
        so the plugin's alias helper resolves without touching disk."""
        import sys
        import types

        fake = types.ModuleType("gateway.whatsapp_identity")
        fake.expand_whatsapp_aliases = lambda v: set(mapping.get(str(v), [str(v)]))
        sys.modules.setdefault("gateway", types.ModuleType("gateway"))
        monkeypatch.setitem(sys.modules, "gateway.whatsapp_identity", fake)

    def test_lid_inbound_finds_phone_keyed_handover(
        self, fresh_state, session_store, gateway, monkeypatch
    ):
        from gateway_policy.rules.handover import handover_rule

        # Bridge knows: phone 60173380115 <-> LID 122299244130458.
        # Whichever form the helper is called with, it returns both bare
        # numerics so every JID variant is reachable.
        aliases = {
            "60173380115@s.whatsapp.net": {"60173380115", "122299244130458"},
            "60173380115": {"60173380115", "122299244130458"},
            "122299244130458@lid": {"60173380115", "122299244130458"},
            "122299244130458": {"60173380115", "122299244130458"},
        }
        self._stub_aliases(monkeypatch, aliases)

        # Row was activated under the phone-form JID (e.g. by an earlier
        # trigger_handover call when the bridge reported phone form).
        fresh_state.handovers.activate(
            "whatsapp",
            "60173380115@s.whatsapp.net",
            reason="agent_tool:smoke",
            activated_by="trigger_handover_tool",
            ttl_seconds=600,
        )

        # Now the customer messages and the bridge surfaces the LID form.
        lid_src = FakeSource(
            platform_str="whatsapp",
            chat_type="dm",
            chat_id="122299244130458@lid",
            user_id="122299244130458@lid",
            user_name="Kong",
        )
        event = FakeEvent(text="hi hi", source=lid_src)
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )

        # Pre-fix: handover_rule returned None and the bot replied.
        # Post-fix: rule must silently ingest under the existing handover.
        assert result == {"action": "skip", "reason": "handover_active"}
        assert len(session_store.appended) == 1

    def test_owner_takeback_on_alias_form_deactivates(
        self, fresh_state, session_store, gateway, monkeypatch
    ):
        """/takeback typed by the owner from the LID form must also end a
        handover that was stored under the phone-JID form."""
        from gateway_policy.rules.handover import handover_rule

        aliases = {
            "60173380115@s.whatsapp.net": {"60173380115", "122299244130458"},
            "122299244130458@lid": {"60173380115", "122299244130458"},
        }
        self._stub_aliases(monkeypatch, aliases)

        fresh_state.handovers.activate(
            "whatsapp",
            "60173380115@s.whatsapp.net",
            reason="manual",
            activated_by="trigger_handover_tool",
            ttl_seconds=600,
        )

        owner_src = FakeSource(
            platform_str="whatsapp",
            chat_type="dm",
            chat_id="122299244130458@lid",
            user_id="122299244130458@lid",
            user_name="Owner",
        )
        event = FakeEvent(
            text="/takeback",
            source=owner_src,
            metadata={"whatsapp_from_owner": True},
        )
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "handover_exit"}
        # Original phone-form row removed — not a stray new row under LID.
        assert fresh_state.handovers.get(
            "whatsapp", "60173380115@s.whatsapp.net"
        ) is None
        assert fresh_state.handovers.get(
            "whatsapp", "122299244130458@lid"
        ) is None

    def test_owner_extend_touches_existing_alias_row(
        self, fresh_state, session_store, gateway, monkeypatch
    ):
        """Owner-implicit TTL slide must update the existing alias row,
        not create a parallel row under the inbound LID form."""
        from gateway_policy.rules.handover import handover_rule

        aliases = {
            "60173380115@s.whatsapp.net": {"60173380115", "122299244130458"},
            "122299244130458@lid": {"60173380115", "122299244130458"},
        }
        self._stub_aliases(monkeypatch, aliases)

        fresh_state.handovers.activate(
            "whatsapp",
            "60173380115@s.whatsapp.net",
            reason="manual",
            activated_by="trigger_handover_tool",
            ttl_seconds=10,  # near-expiry to make the slide observable
        )
        before = fresh_state.handovers.get("whatsapp", "60173380115@s.whatsapp.net")

        owner_src = FakeSource(
            platform_str="whatsapp",
            chat_type="dm",
            chat_id="122299244130458@lid",
            user_id="122299244130458@lid",
            user_name="Owner",
        )
        event = FakeEvent(
            text="ok handling",
            source=owner_src,
            metadata={"whatsapp_from_owner": True},
        )
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "handover_owner_extend"}

        after = fresh_state.handovers.get("whatsapp", "60173380115@s.whatsapp.net")
        assert after is not None
        assert after.activated_by == "trigger_handover_tool"
        assert (after.expires_at or 0) > (before.expires_at or 0) + 500
        # No parallel LID-keyed row.
        assert fresh_state.handovers.get(
            "whatsapp", "122299244130458@lid"
        ) is None

    def test_trigger_handover_reuses_existing_alias_row(
        self, fresh_state, gateway, monkeypatch
    ):
        """The agent-driven trigger_handover tool must also detect an
        existing alias row and reuse its chat_id, instead of creating a
        sibling row under the inbound form (which would split TTLs and
        re-leak past the next variant flip)."""
        import json
        from gateway_policy.tools.trigger_handover import make_trigger_handover_tool

        aliases = {
            "60173380115@s.whatsapp.net": {"60173380115", "122299244130458"},
            "122299244130458@lid": {"60173380115", "122299244130458"},
        }
        self._stub_aliases(monkeypatch, aliases)

        # Stale row keyed by the phone-form JID.
        fresh_state.handovers.activate(
            "whatsapp",
            "60173380115@s.whatsapp.net",
            reason="agent_tool:earlier",
            activated_by="trigger_handover_tool",
            ttl_seconds=600,
        )

        # Agent now invokes the tool from a session that surfaces the LID.
        session_key = "agent:main:whatsapp:dm:122299244130458@lid"
        fresh_state.active_sessions[session_key] = (
            "whatsapp", "122299244130458@lid", "Kong", gateway,
        )

        _, handler = make_trigger_handover_tool(lambda: fresh_state)
        result = json.loads(handler({"reason": "follow-up"}, task_id=session_key))
        assert result["ok"] is True
        # The reused row should be reflected in the tool's return payload.
        assert result["chat_id"] == "60173380115@s.whatsapp.net"
        # Exactly one row in the table — no LID sibling.
        assert len(fresh_state.handovers.list_active()) == 1


# ---------------------------------------------------------------------------
# touch() / sliding TTL
# ---------------------------------------------------------------------------

class TestTouch:
    def test_touch_on_cold_row_is_noop(self, fresh_state, src_dm):
        """No row -> touch returns False, no SQL effect, no row created."""
        ok = fresh_state.handovers.touch("whatsapp", src_dm.chat_id, ttl_seconds=600)
        assert ok is False
        assert fresh_state.handovers.get("whatsapp", src_dm.chat_id) is None

    def test_touch_on_hot_row_pushes_expires_at_forward(self, fresh_state, src_dm):
        """Touch slides expires_at to now + ttl regardless of original TTL."""
        fresh_state.handovers.activate(
            "whatsapp",
            src_dm.chat_id,
            reason="manual",
            activated_by="test",
            ttl_seconds=10,  # tiny initial TTL so we can detect the slide
        )
        original = fresh_state.handovers.get("whatsapp", src_dm.chat_id)
        assert original is not None and original.expires_at is not None

        ok = fresh_state.handovers.touch("whatsapp", src_dm.chat_id, ttl_seconds=600)
        assert ok is True
        bumped = fresh_state.handovers.get("whatsapp", src_dm.chat_id)
        assert bumped is not None and bumped.expires_at is not None
        # The new expires_at must be at least ~9 minutes in the future
        # (the original TTL was 10s, so any sane "slide forward" beats it).
        assert bumped.expires_at > original.expires_at + 500

    def test_touch_on_no_ttl_row_is_noop(self, fresh_state, src_dm):
        """Permanent handover (expires_at IS NULL) is left alone — sliding
        a TTL onto a row the operator chose to keep open forever would be
        a surprise.  This case is rare in practice (timeout_minutes=0)
        but pinning it keeps future refactors honest."""
        fresh_state.handovers.activate(
            "whatsapp",
            src_dm.chat_id,
            reason="manual",
            activated_by="test",
            ttl_seconds=None,
        )
        ok = fresh_state.handovers.touch("whatsapp", src_dm.chat_id, ttl_seconds=600)
        assert ok is False
        row = fresh_state.handovers.get("whatsapp", src_dm.chat_id)
        assert row is not None and row.expires_at is None


# ---------------------------------------------------------------------------
# Implicit owner activation / TTL slide via metadata flag
# ---------------------------------------------------------------------------

class TestOwnerImplicit:
    def _owner_event(self, src, text="hello", flag=True):
        return FakeEvent(
            text=text,
            source=src,
            metadata={"whatsapp_from_owner": True} if flag else {},
        )

    def test_owner_inbound_on_cold_chat_activates_without_notify(
        self, fresh_state, src_dm, session_store, gateway
    ):
        """Owner types in a cold customer chat → activate handover with
        activated_by='owner_implicit', no Telegram notify (the owner is
        already the one in the chat)."""
        from gateway_policy.rules.handover import handover_rule
        from gateway.config import Platform

        event = self._owner_event(src_dm, "got it, will sort it out")
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "handover_owner_activate"}

        row = fresh_state.handovers.get("whatsapp", src_dm.chat_id)
        assert row is not None
        assert row.activated_by == "owner_implicit"
        assert row.reason == "owner_reply"
        assert row.expires_at is not None

        # No owner notification: adapter.sent must be empty.
        assert gateway.adapters[Platform("whatsapp")].sent == []
        # Silent ingest happened so the transcript records the owner's text.
        assert len(session_store.appended) == 1

    def test_owner_inbound_on_hot_chat_slides_ttl_no_double_activate(
        self, fresh_state, src_dm, session_store, gateway
    ):
        """Owner types in a chat with an active handover → touch the TTL,
        keep activated_by intact, no notify, no double-activate."""
        from gateway_policy.rules.handover import handover_rule
        from gateway.config import Platform

        fresh_state.handovers.activate(
            "whatsapp",
            src_dm.chat_id,
            reason="agent_tool:scope mismatch",
            activated_by="trigger_handover_tool",
            ttl_seconds=10,  # near-expiry, so touch() effect is observable
        )
        before = fresh_state.handovers.get("whatsapp", src_dm.chat_id)

        event = self._owner_event(src_dm, "ok handling it")
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "handover_owner_extend"}

        after = fresh_state.handovers.get("whatsapp", src_dm.chat_id)
        assert after is not None
        # activated_by stays as the originator — touch must not overwrite it.
        assert after.activated_by == "trigger_handover_tool"
        assert after.reason == "agent_tool:scope mismatch"
        # TTL slid forward (config.timeout_minutes=10 in fresh_state).
        assert after.expires_at is not None
        assert after.expires_at > before.expires_at + 500
        assert gateway.adapters[Platform("whatsapp")].sent == []

    def test_customer_inbound_does_not_touch_ttl(
        self, fresh_state, src_dm, session_store, gateway
    ):
        """Customer-side inbound during active handover keeps the existing
        silent-ingest behavior — the metadata flag is absent so touch()
        must not be called."""
        from gateway_policy.rules.handover import handover_rule

        fresh_state.handovers.activate(
            "whatsapp",
            src_dm.chat_id,
            reason="manual",
            activated_by="trigger_handover_tool",
            ttl_seconds=10,
        )
        before = fresh_state.handovers.get("whatsapp", src_dm.chat_id)

        event = self._owner_event(src_dm, "still here?", flag=False)
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result == {"action": "skip", "reason": "handover_active"}

        after = fresh_state.handovers.get("whatsapp", src_dm.chat_id)
        # expires_at unchanged within float tolerance.
        assert after is not None and before is not None
        assert abs((after.expires_at or 0) - (before.expires_at or 0)) < 0.01

    def test_bot_outbound_without_flag_is_noop(
        self, fresh_state, src_dm, session_store, gateway
    ):
        """A bot's own outbound (which the bridge LRU would have caught
        upstream) reaches the rule with no flag set — must behave like a
        plain customer inbound, not trigger any owner-implicit branch."""
        from gateway_policy.rules.handover import handover_rule

        # No active handover → cold path returns None.
        event = self._owner_event(src_dm, "bot reply text", flag=False)
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result is None
        assert fresh_state.handovers.get("whatsapp", src_dm.chat_id) is None


# ---------------------------------------------------------------------------
# trigger_handover tool
# ---------------------------------------------------------------------------

class TestTriggerHandoverTool:
    def test_activates_via_session_key_lookup(self, fresh_state, src_dm, gateway):
        from gateway_policy.tools.trigger_handover import make_trigger_handover_tool

        # Stash session context as the hook would.
        session_key = gateway._session_key_for_source(src_dm)
        fresh_state.active_sessions[session_key] = (
            "whatsapp", src_dm.chat_id, src_dm.user_name, gateway,
        )

        schema, handler = make_trigger_handover_tool(lambda: fresh_state)
        result_json = handler(
            {"reason": "Customer asked for custom design quote"},
            task_id=session_key,
        )
        result = json.loads(result_json)
        assert result["ok"] is True
        assert result["platform"] == "whatsapp"
        assert result["chat_id"] == src_dm.chat_id
        assert fresh_state.handovers.is_active("whatsapp", src_dm.chat_id)

    def test_activates_via_session_id_lookup(self, fresh_state, src_dm, gateway):
        """Production path: gateway forwards ``task_id=session_id`` (e.g.
        ``20260427_133748_e36f7ec9``), not the routing session_key. The hook
        stashes by both — verify the file-style session_id resolves cleanly.
        """
        from gateway_policy.tools.trigger_handover import make_trigger_handover_tool

        session_id = "20260427_133748_e36f7ec9"
        fresh_state.active_sessions[session_id] = (
            "whatsapp", src_dm.chat_id, src_dm.user_name, gateway,
        )

        schema, handler = make_trigger_handover_tool(lambda: fresh_state)
        result_json = handler(
            {"reason": "Customer asked for custom design quote"},
            task_id=session_id,
        )
        result = json.loads(result_json)
        assert result["ok"] is True
        assert result["platform"] == "whatsapp"
        assert result["chat_id"] == src_dm.chat_id
        assert fresh_state.handovers.is_active("whatsapp", src_dm.chat_id)

    def test_falls_back_to_session_key_parsing(self, fresh_state, gateway):
        """When active_sessions stash is missing, parses task_id."""
        from gateway_policy.tools.trigger_handover import make_trigger_handover_tool

        schema, handler = make_trigger_handover_tool(lambda: fresh_state)
        task_id = "agent:main:whatsapp:dm:99999@s.whatsapp.net"
        result_json = handler({"reason": "test"}, task_id=task_id)
        result = json.loads(result_json)
        assert result["ok"] is True
        assert result["platform"] == "whatsapp"
        assert result["chat_id"] == "99999@s.whatsapp.net"

    def test_missing_reason_is_error(self, fresh_state):
        from gateway_policy.tools.trigger_handover import make_trigger_handover_tool
        schema, handler = make_trigger_handover_tool(lambda: fresh_state)
        result = json.loads(handler({}, task_id="agent:main:whatsapp:dm:x"))
        assert result["ok"] is False
        assert result["error_code"] == "missing_reason"

    def test_disabled_handover_rejects_tool_call(self, fresh_state):
        from gateway_policy.tools.trigger_handover import make_trigger_handover_tool
        fresh_state.config.handover.enabled = False
        schema, handler = make_trigger_handover_tool(lambda: fresh_state)
        result = json.loads(handler({"reason": "x"}, task_id="agent:main:whatsapp:dm:y"))
        assert result["ok"] is False
        assert result["error_code"] == "handover_disabled"

    def test_pre_dispatch_hook_stashes_by_session_id(
        self, fresh_state, src_dm, gateway, monkeypatch
    ):
        """End-to-end regression: the hook must stash session context under
        the per-session file id so the tool finds it when the agent forwards
        ``task_id=session_id`` (not session_key)."""
        import gateway_policy as gp

        monkeypatch.setattr(gp, "_state", fresh_state)

        session_key = gateway._session_key_for_source(src_dm)
        session_id = "20260427_140000_abcdef12"

        class _Entry:
            def __init__(self, sid):
                self.session_id = sid

        class _Store:
            def __init__(self):
                self._entries = {session_key: _Entry(session_id)}

            def _ensure_loaded(self):
                pass

        gp._pre_gateway_dispatch(
            event=FakeEvent(text="hi", source=src_dm),
            gateway=gateway,
            session_store=_Store(),
        )

        assert session_key in fresh_state.active_sessions
        assert session_id in fresh_state.active_sessions
        cached_by_sid = fresh_state.active_sessions[session_id]
        assert cached_by_sid[0] == "whatsapp"
        assert cached_by_sid[1] == src_dm.chat_id


# ---------------------------------------------------------------------------
# format_chat_link helper
# ---------------------------------------------------------------------------

class TestFormatChatLink:
    def test_whatsapp_lid_with_mapping_returns_canonical_phone(self, monkeypatch):
        """LID with a bridge mapping must resolve to the canonical phone."""
        from gateway_policy import notify

        # Inject a fake gateway.whatsapp_identity that maps the LID to a phone.
        import sys
        import types

        fake_mod = types.ModuleType("gateway.whatsapp_identity")
        fake_mod.canonical_whatsapp_identifier = lambda v: (
            "60173380115" if "@lid" in str(v) else str(v)
        )
        sys.modules.setdefault("gateway", types.ModuleType("gateway"))
        monkeypatch.setitem(sys.modules, "gateway.whatsapp_identity", fake_mod)

        phone, link = notify.format_chat_link("whatsapp", "122299244130458@lid")
        assert phone == "60173380115"
        assert link == "https://wa.me/60173380115"

    def test_whatsapp_phone_jid_returns_phone_and_wame_link(self, monkeypatch):
        """A bare phone JID (no LID) just gets its suffix stripped."""
        import sys
        import types

        from gateway_policy import notify

        fake_mod = types.ModuleType("gateway.whatsapp_identity")
        # Helper returns the JID untouched when it's already a phone.
        fake_mod.canonical_whatsapp_identifier = lambda v: str(v)
        sys.modules.setdefault("gateway", types.ModuleType("gateway"))
        monkeypatch.setitem(sys.modules, "gateway.whatsapp_identity", fake_mod)

        phone, link = notify.format_chat_link(
            "whatsapp", "60173380115@s.whatsapp.net"
        )
        assert phone == "60173380115"
        assert link == "https://wa.me/60173380115"

    def test_whatsapp_canonical_helper_import_error_strips_suffix(self, monkeypatch):
        """If neither canonical helper is importable, fall back to a
        last-resort suffix strip so we still produce a usable wa.me link."""
        from unittest.mock import patch

        from gateway_policy import notify

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in ("gateway.whatsapp_identity", "gateway.session"):
                raise ImportError(f"simulated: {name} unavailable")
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import):
            phone, link = notify.format_chat_link(
                "whatsapp", "60173380115@s.whatsapp.net"
            )
        assert phone == "60173380115"
        assert link == "https://wa.me/60173380115"

    def test_whatsapp_lid_no_mapping_strips_to_digits(self, monkeypatch):
        """LID with no mapping: canonical helper returns it unchanged.
        We still strip ``@lid`` so the wa.me link gets pure digits (best
        effort — owner can copy the digits even if wa.me doesn't resolve)."""
        import sys
        import types

        from gateway_policy import notify

        fake_mod = types.ModuleType("gateway.whatsapp_identity")
        fake_mod.canonical_whatsapp_identifier = lambda v: str(v)
        sys.modules.setdefault("gateway", types.ModuleType("gateway"))
        monkeypatch.setitem(sys.modules, "gateway.whatsapp_identity", fake_mod)

        phone, link = notify.format_chat_link("whatsapp", "122299244130458@lid")
        assert phone == "122299244130458"
        assert link == "https://wa.me/122299244130458"

    def test_telegram_numeric_id_returns_tg_link(self):
        from gateway_policy import notify

        phone, link = notify.format_chat_link("telegram", "640466638")
        assert phone == "640466638"
        assert link == "tg://user?id=640466638"

    def test_unknown_platform_returns_raw_chat_id(self):
        from gateway_policy import notify

        for plat in ("discord", "matrix", "bluebubbles", "api_server", "unknown"):
            phone, link = notify.format_chat_link(plat, "abc-123")
            assert phone == "abc-123", plat
            assert link == "abc-123", plat

    def test_empty_chat_id_returns_safe_defaults(self):
        from gateway_policy import notify

        for plat in ("whatsapp", "telegram", "discord"):
            assert notify.format_chat_link(plat, "") == ("", "")
            assert notify.format_chat_link(plat, None) == ("", "")  # type: ignore[arg-type]

    def test_none_platform_does_not_raise(self):
        from gateway_policy import notify

        phone, link = notify.format_chat_link(None, "abc")  # type: ignore[arg-type]
        assert phone == "abc"
        assert link == "abc"


# ---------------------------------------------------------------------------
# trigger_handover token rendering
# ---------------------------------------------------------------------------

class TestNotifyTokens:
    def test_activate_message_includes_phone_and_link(
        self, fresh_state, src_dm, gateway, monkeypatch
    ):
        """End-to-end: handler should format the configured template with
        the new ``{customer_phone}`` and ``{customer_link}`` tokens populated
        from the canonicalisation helper."""
        import sys
        import types

        from gateway_policy.tools.trigger_handover import make_trigger_handover_tool

        # Stub canonical helper -> known phone.
        fake_mod = types.ModuleType("gateway.whatsapp_identity")
        fake_mod.canonical_whatsapp_identifier = lambda v: "60173380115"
        sys.modules.setdefault("gateway", types.ModuleType("gateway"))
        monkeypatch.setitem(sys.modules, "gateway.whatsapp_identity", fake_mod)

        # Owner on whatsapp (gateway fixture only loads whatsapp adapter).
        fresh_state.config.handover.owner.platform = "whatsapp"
        fresh_state.config.handover.owner.chat_id = "60111111111@s.whatsapp.net"
        fresh_state.config.handover.notify_on_activate = (
            "Handover: {customer_name} ({platform} {customer_phone})\n"
            "Reason: {reason}\n"
            "Chat: {customer_link}"
        )

        session_key = gateway._session_key_for_source(src_dm)
        fresh_state.active_sessions[session_key] = (
            "whatsapp", "122299244130458@lid", "Kong", gateway,
        )

        _, handler = make_trigger_handover_tool(lambda: fresh_state)
        result = json.loads(handler({"reason": "smoke"}, task_id=session_key))
        assert result["ok"] is True
        assert result["owner_notified"] is True

        # Pull the message that was scheduled to the adapter.
        from gateway.config import Platform
        adapter = gateway.adapters[Platform("whatsapp")]
        # asyncio.run already returned by notify_owner -> message present.
        assert adapter.sent, "owner adapter never received the message"
        _, message = adapter.sent[-1]
        assert "wa.me/60173380115" in message
        assert "60173380115" in message
        assert "@lid" not in message
        assert "Reason: smoke" in message

    def test_activate_message_legacy_template_still_works(
        self, fresh_state, src_dm, gateway
    ):
        """Profiles still using the old ``{chat_id}`` template must keep
        working unchanged — the handler now passes extra kwargs but old
        tokens still render."""
        from gateway_policy.tools.trigger_handover import make_trigger_handover_tool

        fresh_state.config.handover.owner.platform = "whatsapp"
        fresh_state.config.handover.owner.chat_id = "60111111111@s.whatsapp.net"
        fresh_state.config.handover.notify_on_activate = (
            "Handover: {customer_name} ({chat_id}). Reason: {reason}"
        )

        session_key = gateway._session_key_for_source(src_dm)
        fresh_state.active_sessions[session_key] = (
            "whatsapp", src_dm.chat_id, "Kong", gateway,
        )

        _, handler = make_trigger_handover_tool(lambda: fresh_state)
        result = json.loads(handler({"reason": "x"}, task_id=session_key))
        assert result["ok"] is True
        from gateway.config import Platform
        adapter = gateway.adapters[Platform("whatsapp")]
        assert adapter.sent
        _, message = adapter.sent[-1]
        assert message == f"Handover: Kong ({src_dm.chat_id}). Reason: x"


# ---------------------------------------------------------------------------
# rule pipeline
# ---------------------------------------------------------------------------

class TestPipeline:
    def test_first_acting_rule_wins(self, fresh_state, src_dm, session_store, gateway):
        from gateway_policy.rules.base import (
            clear_rules,
            register_rule,
            run_pipeline,
        )

        clear_rules()
        register_rule(lambda **kw: {"action": "skip", "reason": "early"}, priority=10, name="early")
        register_rule(lambda **kw: {"action": "rewrite", "text": "later"}, priority=20, name="late")

        result = run_pipeline(
            event=FakeEvent(text="hi", source=src_dm),
            gateway=gateway, session_store=session_store, state=fresh_state,
        )
        assert result == {"action": "skip", "reason": "early"}

    def test_rule_exception_is_caught_and_pipeline_continues(
        self, fresh_state, src_dm, session_store, gateway
    ):
        from gateway_policy.rules.base import (
            clear_rules,
            register_rule,
            run_pipeline,
        )

        clear_rules()
        def boom(**kw): raise RuntimeError("nope")
        register_rule(boom, priority=10, name="boom")
        register_rule(lambda **kw: {"action": "allow"}, priority=20, name="ok")

        result = run_pipeline(
            event=FakeEvent(text="hi", source=src_dm),
            gateway=gateway, session_store=session_store, state=fresh_state,
        )
        assert result == {"action": "allow"}

    def test_invalid_action_dropped(self, fresh_state, src_dm, session_store, gateway):
        from gateway_policy.rules.base import (
            clear_rules,
            register_rule,
            run_pipeline,
        )

        clear_rules()
        register_rule(lambda **kw: {"action": "explode"}, priority=10, name="bad")
        register_rule(lambda **kw: None, priority=20, name="passthru")

        # Bad action ignored, no rule acts → None
        result = run_pipeline(
            event=FakeEvent(text="hi", source=src_dm),
            gateway=gateway, session_store=session_store, state=fresh_state,
        )
        assert result is None

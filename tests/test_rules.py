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

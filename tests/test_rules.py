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


# ---------------------------------------------------------------------------
# handover
# ---------------------------------------------------------------------------

class TestHandover:
    def test_disabled_returns_none(self, fresh_state, src_dm, session_store, gateway):
        from gateway_policy.rules.handover import handover_rule
        fresh_state.config.handover.enabled = False
        event = FakeEvent(text="speak to a human", source=src_dm)
        assert handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        ) is None

    def test_phrase_triggers_activation_and_silent_ingest(
        self, fresh_state, src_dm, session_store, gateway
    ):
        from gateway_policy.rules.handover import handover_rule
        event = FakeEvent(text="please let me speak to a human now", source=src_dm)
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result["action"] == "skip"
        assert "phrase" in result["reason"]
        # Active in DB.
        assert fresh_state.handovers.is_active("whatsapp", src_dm.chat_id)
        # Customer message went to transcript.
        assert len(session_store.appended) == 1

    def test_active_handover_silent_ingests_subsequent_messages(
        self, fresh_state, src_dm, session_store, gateway
    ):
        from gateway_policy.rules.handover import handover_rule
        # Pre-activate.
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
        event = FakeEvent(text="speak to a human", source=tg)
        # Handover platforms = ['whatsapp'] only.
        assert handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        ) is None
        assert not fresh_state.handovers.is_active("telegram", "-100")

    def test_phrase_match_case_insensitive(self, fresh_state, src_dm, session_store, gateway):
        from gateway_policy.rules.handover import handover_rule
        event = FakeEvent(text="SPEAK TO A HUMAN", source=src_dm)
        result = handover_rule(
            event=event, gateway=gateway, session_store=session_store, state=fresh_state
        )
        assert result["action"] == "skip"
        assert fresh_state.handovers.is_active("whatsapp", src_dm.chat_id)


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

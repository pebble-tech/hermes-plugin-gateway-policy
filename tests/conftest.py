"""Shared fixtures for gateway-policy plugin tests.

The plugin is loaded under the alias ``gateway_policy`` by the
repo-root ``conftest.py``; this file only defines test fixtures and
fake Hermes surfaces (source, session store, gateway).
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

import pytest


# ---- helpers used as fixtures ----------------------------------------------

@dataclass
class _FakeSource:
    platform_str: str = "whatsapp"
    user_id: str = "15551234567@s.whatsapp.net"
    chat_id: str = "120363xyz@g.us"
    user_name: str = "Customer"
    chat_type: str = "group"
    thread_id: Optional[str] = None
    platform: Any = None

    def __post_init__(self):
        self.platform = SimpleNamespace(value=self.platform_str)


class _FakeSessionStore:
    def __init__(self):
        self.appended = []
        self.updated = []

    def get_or_create_session(self, source):
        return SimpleNamespace(
            session_id=f"s_{source.chat_id}",
            session_key=f"agent:main:{source.platform.value}:{source.chat_type}:{source.chat_id}",
        )

    def append_to_transcript(self, session_id, message):
        self.appended.append((session_id, message))

    def update_session(self, session_key):
        self.updated.append(session_key)


class _FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, message):
        self.sent.append((chat_id, message))


class _FakeGateway:
    def __init__(self, platforms=("whatsapp",)):
        # Stub Platform enum so notify_owner can resolve it.
        if "gateway.config" not in sys.modules:
            cfg_mod = types.ModuleType("gateway.config")

            class _Platform:
                _members: dict = {}

                def __init__(self, name):
                    self._name = name
                    self.value = name

                def __eq__(self, other):
                    return isinstance(other, _Platform) and other._name == self._name

                def __hash__(self):
                    return hash(self._name)

            def _factory(name):
                if name not in _Platform._members:
                    _Platform._members[name] = _Platform(name)
                return _Platform._members[name]

            cfg_mod.Platform = _factory
            sys.modules.setdefault("gateway", types.ModuleType("gateway"))
            sys.modules["gateway.config"] = cfg_mod

        from gateway.config import Platform
        self.adapters = {Platform(p): _FakeAdapter() for p in platforms}

    def _session_key_for_source(self, source):
        plat = source.platform.value
        return f"agent:main:{plat}:{source.chat_type}:{source.chat_id}"


@pytest.fixture
def src_dm():
    return _FakeSource(
        chat_type="dm",
        chat_id="15551234567@s.whatsapp.net",
        user_id="15551234567@s.whatsapp.net",
    )


@pytest.fixture
def src_group():
    return _FakeSource(chat_type="group", chat_id="120363aaa@g.us", user_name="Alice")


@pytest.fixture
def session_store():
    return _FakeSessionStore()


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    from gateway_policy.config import (
        HandoverConfig,
        HandoverTriggers,
        ListenOnlyConfig,
        OwnerConfig,
        PolicyConfig,
        ToolConfig,
    )
    from gateway_policy import state as state_mod

    monkeypatch.setattr(state_mod, "_state_dir", lambda: tmp_path)

    cfg = PolicyConfig(
        enabled=True,
        listen_only=ListenOnlyConfig(window_seconds=60, require_mention=True, buffer_max=10),
        handover=HandoverConfig(
            enabled=True,
            platforms=["whatsapp"],
            owner=OwnerConfig(platform="whatsapp", chat_id="60111111111@s.whatsapp.net"),
            triggers=HandoverTriggers(phrases=["speak to a human", "talk to owner"]),
            timeout_minutes=10,
            tool=ToolConfig(enabled=True),
        ),
    )
    return state_mod.PolicyState(config=cfg)


@pytest.fixture
def gateway():
    return _FakeGateway(platforms=("whatsapp",))

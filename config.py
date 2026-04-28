"""Profile-local config loader for the gateway-policy plugin.

Reads the `plugins.gateway-policy.*` section from the active profile's
config.yaml via Hermes's `load_config()`. Profile isolation is handled by
Hermes's HERMES_HOME resolution (each profile has its own config.yaml).

Config shape:

    plugins:
      gateway-policy:
        enabled: true

        listen_only:
          window_seconds: 120
          require_mention: true      # only tag triggers reply during the window
          buffer_ambient: true       # false = drop pre-tag / post-window msgs
          buffer_max: 50
          rewrite_header: "Recent chat context:"
          mention_patterns:          # optional; auto-inherits
            - "(?i)^bot\\b"          #   `whatsapp.mention_patterns` if omitted
          chats:
            - { platform: whatsapp, chat_id: "120363..." }
            - { platform: telegram, chat_id: "-100123..." }

        handover:
          enabled: true
          platforms: [whatsapp]
          owner:
            platform: whatsapp
            chat_id: "60123456789@s.whatsapp.net"
          timeout_minutes: 60
          exit_command: "/handover"
          tool:
            enabled: true

Takeover activation is agent-driven only: the `trigger_takeover` tool is
the sole entry point. Phrase / LLM-classifier gateway-side triggers were
removed — they were unreliable in multi-language deployments and
duplicated the main agent's own context-aware judgement.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Tuple

logger = logging.getLogger("gateway-policy.config")


@dataclass(frozen=True)
class ChatRef:
    """A platform-qualified chat identifier."""

    platform: str
    chat_id: str
    name: Optional[str] = None

    @property
    def key(self) -> Tuple[str, str]:
        return (self.platform, self.chat_id)


@dataclass
class OwnerConfig:
    platform: Optional[str] = None
    chat_id: Optional[str] = None


@dataclass
class ToolConfig:
    enabled: bool = True


@dataclass
class HandoverConfig:
    enabled: bool = False
    platforms: List[str] = field(default_factory=lambda: ["whatsapp"])
    owner: OwnerConfig = field(default_factory=OwnerConfig)
    timeout_minutes: int = 60
    exit_command: str = "/handover"
    tool: ToolConfig = field(default_factory=ToolConfig)


@dataclass
class ListenOnlyConfig:
    window_seconds: int = 120
    require_mention: bool = True
    buffer_max: int = 50
    rewrite_header: str = "Recent chat context:"
    chats: List[ChatRef] = field(default_factory=list)
    # Compiled regexes applied to event.text when detecting a bot mention.
    # Auto-populated from the adapter's `whatsapp.mention_patterns` so users
    # don't have to duplicate config. Plugin-level override available at
    # `plugins.gateway-policy.listen_only.mention_patterns`.
    mention_patterns: List[Pattern[str]] = field(default_factory=list)
    # When True (default), pre-tag ambient group messages are appended to an
    # in-memory buffer and silently ingested into the session transcript so a
    # later mention can collapse them into context. Set False for a pure
    # "tag-to-reply + follow-up window" flow where untagged messages outside
    # the window are dropped completely (no buffer, no transcript write).
    buffer_ambient: bool = True


@dataclass
class PolicyConfig:
    enabled: bool = True
    listen_only: ListenOnlyConfig = field(default_factory=ListenOnlyConfig)
    handover: HandoverConfig = field(default_factory=HandoverConfig)


def _parse_chat_refs(raw: Any) -> List[ChatRef]:
    """Accept list of dicts `[{platform, chat_id, name?}, ...]`."""
    out: List[ChatRef] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        platform = str(entry.get("platform") or "").strip().lower()
        chat_id = str(entry.get("chat_id") or "").strip()
        if not platform or not chat_id:
            continue
        name = entry.get("name")
        out.append(ChatRef(platform=platform, chat_id=chat_id, name=name))
    return out


def _compile_patterns(raw: Any) -> List[Pattern[str]]:
    """Compile a list/str of regex strings, logging and skipping invalid ones."""
    if isinstance(raw, str):
        candidates: List[str] = [raw]
    elif isinstance(raw, list):
        candidates = [str(p) for p in raw if p]
    else:
        return []

    compiled: List[Pattern[str]] = []
    for pattern in candidates:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            logger.warning("skipping invalid mention_pattern %r: %s", pattern, exc)
    return compiled


def _parse_listen_only(raw: Dict[str, Any]) -> ListenOnlyConfig:
    cfg = ListenOnlyConfig()
    if not isinstance(raw, dict):
        return cfg
    cfg.window_seconds = int(raw.get("window_seconds", cfg.window_seconds) or 0)
    cfg.require_mention = bool(raw.get("require_mention", cfg.require_mention))
    cfg.buffer_max = int(raw.get("buffer_max", cfg.buffer_max) or cfg.buffer_max)
    cfg.rewrite_header = str(raw.get("rewrite_header", cfg.rewrite_header))
    cfg.chats = _parse_chat_refs(raw.get("chats", []))
    # Plugin-level override; auto-bridge from the adapter happens in
    # load_policy_config() below when this list is still empty.
    cfg.mention_patterns = _compile_patterns(raw.get("mention_patterns"))
    cfg.buffer_ambient = bool(raw.get("buffer_ambient", cfg.buffer_ambient))
    return cfg


def _parse_handover(raw: Dict[str, Any]) -> HandoverConfig:
    cfg = HandoverConfig()
    if not isinstance(raw, dict):
        return cfg
    cfg.enabled = bool(raw.get("enabled", cfg.enabled))
    platforms = raw.get("platforms")
    if isinstance(platforms, list) and platforms:
        cfg.platforms = [str(p).strip().lower() for p in platforms if str(p).strip()]

    owner_raw = raw.get("owner") or {}
    if isinstance(owner_raw, dict):
        cfg.owner = OwnerConfig(
            platform=(str(owner_raw.get("platform") or "").strip().lower() or None),
            chat_id=(str(owner_raw.get("chat_id") or "").strip() or None),
        )

    # `triggers:` (phrases / llm_classifier) used to live here. Removed —
    # takeover is now activated only via the `trigger_takeover` agent tool.
    # Any leftover `triggers:` block in profile config.yaml is ignored.

    cfg.timeout_minutes = int(raw.get("timeout_minutes", cfg.timeout_minutes) or 0)
    cfg.exit_command = str(raw.get("exit_command", cfg.exit_command))

    tool_raw = raw.get("tool") or {}
    if isinstance(tool_raw, dict):
        cfg.tool = ToolConfig(enabled=bool(tool_raw.get("enabled", True)))
    return cfg


def load_policy_config() -> PolicyConfig:
    """Load the plugin's configuration from profile config.yaml.

    Never raises; returns defaults on any error so a misconfiguration
    cannot break the gateway loop.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
    except Exception as exc:
        logger.warning("load_config() failed, using defaults: %s", exc)
        return PolicyConfig()

    section = (config.get("plugins") or {}).get("gateway-policy") or {}
    if not isinstance(section, dict):
        return PolicyConfig()

    policy = PolicyConfig(enabled=bool(section.get("enabled", True)))
    policy.listen_only = _parse_listen_only(section.get("listen_only") or {})
    policy.handover = _parse_handover(section.get("handover") or {})

    # If the plugin didn't get explicit mention_patterns, inherit the
    # adapter's WhatsApp `mention_patterns`. This avoids duplicating
    # regexes the operator already configured on the WhatsApp adapter.
    if not policy.listen_only.mention_patterns:
        adapter_raw = (config.get("whatsapp") or {}).get("mention_patterns")
        policy.listen_only.mention_patterns = _compile_patterns(adapter_raw)

    return policy

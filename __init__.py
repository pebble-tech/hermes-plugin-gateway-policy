"""gateway-policy plugin: pre-dispatch message flow patterns.

Patterns supported out of the box:
- listen_only: buffer ambient group messages, collapse into the next tagged
  turn, open a follow-up window so contiguous replies don't require re-tagging.
- handover: silent-ingest customer messages while the owner handles them
  manually. Activation via phrases, optional aux-LLM classifier, or the
  `trigger_handover` tool the agent can call mid-conversation.

Extension API: external plugins may call `register_rule(fn, priority=50)`
to add their own pre-dispatch rules without modifying this plugin.
"""

from __future__ import annotations

import logging

# Why the guard:
# Hermes loads this as ``hermes_plugins.gateway_policy`` — a proper
# package — so relative imports work. Pytest, however, may import this
# file as a standalone module while walking ancestor ``__init__.py`` files
# from the tests/ dir; at that point ``__package__`` is empty and the
# relative imports below would fail. The real plugin is preloaded under
# the ``gateway_policy`` alias by the repo-root ``conftest.py``, so the
# standalone-file path here just needs to be a safe no-op.
if __package__ in (None, ""):
    def register(_ctx):  # type: ignore[misc]
        return None

    __all__ = ["register"]

else:
    from .config import load_policy_config
    from .rules.base import clear_rules, list_rules, register_rule, run_pipeline
    from .rules.handover import handover_rule
    from .rules.listen_only import listen_only_rule
    from .state import PolicyState
    from .tools.trigger_handover import make_trigger_handover_tool

    __all__ = ["register_rule", "run_pipeline", "list_rules"]

    logger = logging.getLogger("gateway-policy")

    _state: PolicyState | None = None

    def get_state() -> PolicyState:
        """Return (and lazily create) the plugin's runtime state singleton."""
        global _state
        if _state is None:
            _state = PolicyState(config=load_policy_config())
        return _state

    def _pre_gateway_dispatch(*, event, gateway, session_store, **_kwargs):
        """Top-level hook entrypoint dispatched by Hermes core."""
        state = get_state()
        if not state.config.enabled:
            return None

        # Stash source-by-session_key so trigger_handover (which only receives
        # task_id == session_key) can recover the platform/chat_id/user_name.
        try:
            source = event.source
            platform = (
                source.platform.value
                if hasattr(source.platform, "value")
                else str(source.platform)
            ).lower()
            chat_id = str(getattr(source, "chat_id", "") or "")
            user_name = str(getattr(source, "user_name", "") or "")
            session_key = gateway._session_key_for_source(source)
            state.active_sessions[session_key] = (platform, chat_id, user_name, gateway)
        except Exception:
            pass

        try:
            return run_pipeline(
                event=event,
                gateway=gateway,
                session_store=session_store,
                state=state,
            )
        except Exception as exc:
            logger.warning("gateway-policy rule pipeline raised: %s", exc)
            return None

    def register(ctx):
        """Plugin entry point called by Hermes's PluginManager."""
        state = get_state()

        # Reset to built-in rules on every register() (safe for reload scenarios).
        clear_rules()
        register_rule(listen_only_rule, priority=50, name="listen_only")
        register_rule(handover_rule, priority=60, name="handover")

        ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)

        if state.config.handover.enabled and state.config.handover.tool.enabled:
            schema, handler = make_trigger_handover_tool(get_state)
            ctx.register_tool(
                name="trigger_handover",
                toolset="gateway_policy",
                schema=schema,
                handler=handler,
                description="Hand the current conversation over to a human owner.",
            )

        logger.info(
            "gateway-policy loaded: listen_only=%d chat(s), handover=%s, tool=%s",
            len(state.config.listen_only.chats),
            state.config.handover.enabled,
            state.config.handover.tool.enabled,
        )

"""Rule pipeline for gateway-policy.

A rule is a callable: `(event, gateway, session_store, state) -> dict | None`
that returns an action dict (`allow`, `skip`, `rewrite`) or None to pass.
"""

from .base import register_rule, run_pipeline, clear_rules, list_rules

__all__ = ["register_rule", "run_pipeline", "clear_rules", "list_rules"]

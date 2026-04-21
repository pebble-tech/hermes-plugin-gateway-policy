"""Rule registration + pipeline execution.

Rules are ordered by `priority` (lower runs first). Ties broken by insertion
order. The first rule returning a non-None action dict wins and stops the
pipeline. Rule exceptions are caught per-rule so one bad rule cannot break
the gateway.

Recommended priority conventions:
    0-29   high-priority overrides (e.g. VIP / global allowlists)
    30-49  profile-specific pre-rules
    50-79  built-in rules (listen_only=50, handover=60)
    80+    observers / logging
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("gateway-policy.rules")

# Action strings accepted by the core pre_gateway_dispatch hook.
_VALID_ACTIONS = {"allow", "skip", "rewrite"}


@dataclass
class _RuleEntry:
    fn: Callable[..., Optional[Dict[str, Any]]]
    priority: int
    name: str
    order: int


_rules: List[_RuleEntry] = []
_insert_counter = 0


def register_rule(
    fn: Callable[..., Optional[Dict[str, Any]]],
    *,
    priority: int = 50,
    name: Optional[str] = None,
) -> None:
    """Register a pre-dispatch rule.

    Args:
        fn: Callable with kwargs (event, gateway, session_store, state).
            Return None to pass; return {"action": ..., ...} to act.
        priority: Lower number runs first (default 50).
        name: Optional human-readable name for logging.
    """
    global _insert_counter
    entry_name = name or getattr(fn, "__name__", repr(fn))
    entry = _RuleEntry(fn=fn, priority=priority, name=entry_name, order=_insert_counter)
    _insert_counter += 1
    _rules.append(entry)
    _rules.sort(key=lambda e: (e.priority, e.order))
    logger.debug("gateway-policy registered rule '%s' priority=%d", entry_name, priority)


def clear_rules() -> None:
    """Clear all registered rules (used on plugin reload)."""
    global _insert_counter
    _rules.clear()
    _insert_counter = 0


def list_rules() -> List[Dict[str, Any]]:
    """Return a list of registered rule descriptors (for debugging)."""
    return [
        {"name": e.name, "priority": e.priority, "order": e.order}
        for e in _rules
    ]


def _validate(result: Any, rule_name: str) -> Optional[Dict[str, Any]]:
    if result is None:
        return None
    if not isinstance(result, dict):
        logger.warning(
            "rule '%s' returned non-dict %r; ignoring", rule_name, type(result).__name__,
        )
        return None
    action = result.get("action")
    if action not in _VALID_ACTIONS:
        logger.warning(
            "rule '%s' returned unknown action %r; ignoring", rule_name, action,
        )
        return None
    if action == "rewrite" and not isinstance(result.get("text"), str):
        logger.warning("rule '%s' rewrite missing 'text'; ignoring", rule_name)
        return None
    return result


def run_pipeline(*, event, gateway, session_store, state) -> Optional[Dict[str, Any]]:
    """Execute all registered rules in priority order.

    Returns the first non-None action dict, or None if no rule acted.
    """
    for entry in _rules:
        try:
            result = entry.fn(
                event=event,
                gateway=gateway,
                session_store=session_store,
                state=state,
            )
        except Exception as exc:
            logger.warning("rule '%s' raised: %s", entry.name, exc)
            continue
        validated = _validate(result, entry.name)
        if validated is not None:
            logger.debug(
                "gateway-policy rule '%s' acted: %s",
                entry.name,
                validated.get("action"),
            )
            return validated
    return None

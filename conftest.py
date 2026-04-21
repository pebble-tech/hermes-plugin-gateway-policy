"""Root-level pytest conftest for hermes-plugin-gateway-policy.

Because Hermes's Git-based installer requires plugin files at the repo
root (including ``__init__.py``), pytest's auto-collector would otherwise
treat the repo root as a package and fail on the plugin's relative
imports. This conftest:

1. Tells pytest to skip the repo-root ``__init__.py`` (it is the plugin
   entry, not a test module).
2. Pre-loads the plugin under the alias ``gateway_policy`` — matching
   how Hermes's PluginManager mounts it as ``hermes_plugins.gateway_policy``
   — so test imports like ``from gateway_policy.config import ...`` work
   identically under pytest and under a real Hermes install.

Runtime Hermes loading is unaffected; this file is only consumed by pytest.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

# Don't let pytest try to collect the plugin's own __init__.py as a test.
collect_ignore = ["__init__.py"]

# Stub hermes_constants so the plugin's import-time
# `from hermes_constants import get_hermes_home` succeeds outside a real
# Hermes install.
if "hermes_constants" not in sys.modules:
    _stub = types.ModuleType("hermes_constants")
    _stub.get_hermes_home = lambda: Path("/tmp/_gw_policy_test_home")
    sys.modules["hermes_constants"] = _stub

# Load the plugin under the alias ``gateway_policy`` (valid Python
# identifier). The repo directory name is hyphenated, which is fine for
# Git but not importable as a Python package.
if "gateway_policy" not in sys.modules:
    _init = _ROOT / "__init__.py"
    _spec = importlib.util.spec_from_file_location(
        "gateway_policy",
        _init,
        submodule_search_locations=[str(_ROOT)],
    )
    _module = importlib.util.module_from_spec(_spec)
    _module.__package__ = "gateway_policy"
    _module.__path__ = [str(_ROOT)]
    sys.modules["gateway_policy"] = _module
    _spec.loader.exec_module(_module)

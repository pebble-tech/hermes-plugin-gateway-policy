# hermes-plugin-gateway-policy

A standalone [Hermes Agent](https://github.com/NousResearch/hermes-agent)
plugin that adds two pre-dispatch message-flow patterns without
touching any business-logic plugin:

1. **listen_only** — buffer ambient group messages silently and
   collapse them into the next tagged turn; open a follow-up window
   so contiguous replies don't require re-tagging. Built for
   customer-service group chats where the bot shouldn't react to
   every message but needs the preceding context when it does.
2. **handover** — silent-ingest customer messages while the owner
   handles the chat manually. Activation via phrases, optional
   aux-LLM classifier, or the `trigger_handover` tool the agent
   itself can call mid-conversation.

Both patterns are profile-agnostic — install once, configure per
profile via `config.yaml`. Works across every gateway platform
(WhatsApp, Telegram, Discord, …) because it operates at the gateway
hook layer, not in any adapter.

---

## Installing

### Option A: Hermes Git installer (recommended)

```bash
hermes plugins install pebble-tech/hermes-plugin-gateway-policy
hermes gateway restart
```

That's the whole install. The CLI clones the repo into
`$HERMES_HOME/plugins/gateway-policy/`, reads `plugin.yaml`, renders
`after-install.md`, then prompts for any required env vars. Restart
the gateway so the new hook + tool are picked up.

Per-profile install (if you use the multi-profile layout):

```bash
HERMES_HOME=~/.hermes/profiles/<profile-name> \
  hermes plugins install pebble-tech/hermes-plugin-gateway-policy
```

Update later with:

```bash
hermes plugins update gateway-policy
```

### Option B: Local checkout (for plugin development)

```bash
git clone git@github.com:pebble-tech/hermes-plugin-gateway-policy.git
ln -s "$(pwd)/hermes-plugin-gateway-policy" \
      ~/.hermes/plugins/gateway_policy
hermes gateway restart
```

Python module imports use underscores, so the symlink **name** must
be `gateway_policy` even though the git repo has hyphens.

### Option C: PyPI entry point (future)

Hermes also auto-discovers plugins installed via `pip` that declare
an entry point under `hermes_agent.plugins`. This distribution path
is not enabled yet — see `pyproject.toml` for the planned hook.

---

## Requirements

- Hermes Agent build with the `pre_gateway_dispatch` plugin hook.
  Verify with:

  ```bash
  python -c "from hermes_cli.plugins import VALID_HOOKS; \
    assert 'pre_gateway_dispatch' in VALID_HOOKS"
  ```

  If this errors, your Hermes build predates the hook — see
  **Core patch** below.

- For listen-only group chats: the chat must be configured so
  ambient (non-tagged) messages reach the gateway. On WhatsApp this
  means listing the chat under `whatsapp.free_response_chats` in
  `config.yaml`. The plugin then takes over filtering via the hook.

---

## Configuration

Copy the `plugins.gateway-policy:` block from
[`config.example.yaml`](./config.example.yaml) into your profile's
`config.yaml` and edit to taste. Minimum viable examples:

**Listen-only on one WhatsApp group:**

```yaml
plugins:
  gateway-policy:
    enabled: true
    listen_only:
      window_seconds: 120
      chats:
        - { platform: whatsapp, chat_id: "120363xxxxxxxxxxxx@g.us" }

whatsapp:
  free_response_chats:
    - "120363xxxxxxxxxxxx@g.us"
```

**Handover on customer DMs:**

```yaml
plugins:
  gateway-policy:
    enabled: true
    handover:
      enabled: true
      platforms: [whatsapp]
      owner:
        platform: whatsapp
        chat_id: "60123456789@s.whatsapp.net"
      triggers:
        phrases: ["speak to a human", "talk to owner"]
      exit_command: "/takeback"
      tool:
        enabled: true
```

Profile isolation is automatic — Hermes resolves
`get_hermes_home()` to the active profile, so each profile gets its
own SQLite state file and configuration.

See `config.example.yaml` for the full option reference.

---

## Agent guidance for `trigger_handover`

The tool description tells the LLM **when** to escalate (explicit
ask for a human, or out-of-scope per operating instructions).
Profile-specific scope rules belong in your `AGENTS.md` /
personality prompt, e.g.:

```markdown
## Out of scope (use trigger_handover)

- Custom design consultations (sketches, color matching, edge cases)
- Refund disputes
- Anything where the customer mentions a complaint or escalation

## In scope (handle yourself)

- Standard product questions
- Quote requests for in-stock SKUs
- Order status checks
```

The tool is intentionally generic — keep business rules in
`AGENTS.md` so this plugin works across profiles unchanged.

---

## Extending the plugin from another plugin

Other plugins can inject their own pre-dispatch rules without
forking this one. Example: a `vip-allowlist` plugin that fast-tracks
specific customers past the listen-only buffer.

```python
# in your-plugin/__init__.py
from hermes_plugins.gateway_policy import register_rule


def vip_rule(*, event, gateway, session_store, state, **_):
    if event.source.user_id in {"15551111111@s.whatsapp.net"}:
        return {"action": "allow"}
    return None


def register(ctx):
    register_rule(vip_rule, priority=10, name="vip_allowlist")
```

Priority conventions:

| Range | Use |
|-------|-----|
| 0-29  | High-priority overrides (VIP, global allowlists) |
| 30-49 | Profile-specific pre-rules |
| 50-79 | Built-in (`listen_only=50`, `handover=60`) |
| 80+   | Observers / logging only |

The first rule returning a non-None action dict wins; remaining
rules are skipped. Exceptions are caught per-rule and logged.

If load order matters (sibling plugins may init before
`gateway-policy`), grab `register_rule` via the plugin manager
instead of importing it at module-load time:

```python
from hermes_cli.plugins import get_plugin_manager

def register(ctx):
    loaded = get_plugin_manager()._plugins.get("gateway-policy")
    if loaded and loaded.module:
        loaded.module.register_rule(my_rule, priority=10)
```

---

## State

- **Persistent (SQLite)**:
  `$HERMES_HOME/workspace/state/gateway-policy/state.db`, one table
  `handovers(platform, chat_id, reason, activated_at, activated_by,
  expires_at, notified)`. Survives gateway restarts.
- **In-memory**: listen-only buffers (`deque`) and follow-up window
  expiries (`dict`). Lost on restart — acceptable since windows are
  short-lived (minutes).

Inspect:

```bash
sqlite3 ~/.hermes/profiles/<name>/workspace/state/gateway-policy/state.db \
  "SELECT * FROM handovers"
```

Force-clear an active handover:

```bash
sqlite3 ~/.hermes/profiles/<name>/workspace/state/gateway-policy/state.db \
  "DELETE FROM handovers WHERE chat_id='...'"
```

---

## Core patch (pending upstream PR)

This plugin needs the `pre_gateway_dispatch` hook in Hermes core. If
your Hermes build predates the hook, you must either:

1. **Carry a minimal fork** until the upstream PR lands. The patch
   is small (~50 lines + tests + docs) and rarely conflicts:
   - Add `"pre_gateway_dispatch"` to `VALID_HOOKS` in
     `hermes_cli/plugins.py`.
   - Add an `invoke_hook("pre_gateway_dispatch", ...)` block in
     `gateway/run.py::_handle_message`, between the internal-event
     check and the auth chain, with action handling for `skip`,
     `rewrite`, and `allow`.
2. **Or wait** for the upstream PR to merge. See the tracking issue
   on this repo.

---

## Repository layout

```
hermes-plugin-gateway-policy/
├── plugin.yaml              # Hermes manifest (name, version, hooks, tools)
├── after-install.md         # rendered by `hermes plugins install`
├── config.example.yaml      # full config reference (paste into profile)
├── __init__.py              # plugin entry: register() + register_rule()
├── config.py                # dataclass config loader
├── state.py                 # PolicyState + SQLite HandoverStore
├── triggers.py              # phrase / LLM classifier helpers
├── notify.py                # owner notification helpers
├── transcript_utils.py      # silent-ingest helpers
├── rules/
│   ├── base.py              # rule registry + pipeline runner
│   ├── listen_only.py
│   └── handover.py
├── tools/
│   └── trigger_handover.py  # tool schema + handler
├── tests/                   # pytest suite (17 tests)
├── pyproject.toml           # dev-tool config (pytest, ruff)
├── LICENSE
├── .gitignore
└── README.md
```

Plugin files sit at the repo root — that's what Hermes's installer
expects (it clones and `mv`'s the whole tree into
`$HERMES_HOME/plugins/<manifest.name>/`).

---

## Limitations

- **Owner replies via the bot's own number**: the WhatsApp bridge
  filters `fromMe` messages, so an owner replying through the bot's
  WhatsApp does *not* appear in the customer's transcript. The
  customer sees the reply, but the bot has no record of it. If the
  owner later `/takeback`s, the bot's view of "what happened during
  handover" is one-sided.
- **Listen-only requires platform-side passthrough**: the plugin
  can't make an adapter forward filtered messages on its own — you
  must list the chat under `<platform>.free_response_chats`.

---

## Development

```bash
git clone git@github.com:pebble-tech/hermes-plugin-gateway-policy.git
cd hermes-plugin-gateway-policy
pip install pytest pytest-asyncio pyyaml
pytest
```

Tests run with an isolated fake gateway / session store, so no
Hermes install is needed.

---

## License

MIT. See [LICENSE](./LICENSE).

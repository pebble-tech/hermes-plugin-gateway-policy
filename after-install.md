# gateway-policy installed

Two pre-dispatch patterns are now available for this profile:

- **listen_only** — buffer ambient group messages; collapse on tag; follow-up window.
- **handover** — silent-ingest while the owner handles the chat manually.

Neither is active until you configure it.

## 1. Confirm the core hook is present

This plugin needs the `pre_gateway_dispatch` hook in Hermes core.
Quick check:

```bash
python -c "from hermes_cli.plugins import VALID_HOOKS; \
  assert 'pre_gateway_dispatch' in VALID_HOOKS, 'hook missing'"
```

If this errors, your Hermes build predates the hook — see
`README.md` → "Core patch" for the fork/PR plan.

## 2. Add a config block to your profile

Open your profile's `config.yaml` and paste the template from
`config.example.yaml` (shipped with this plugin) under a top-level
`plugins.gateway-policy:` key. Then edit to taste.

Minimum viable examples:

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
    - "120363xxxxxxxxxxxx@g.us"   # required: let ambient msgs reach gateway
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
      exit_command: "/takeback"
      tool:
        enabled: true
```

Handover activates only when the agent calls the `trigger_handover`
tool. There are no gateway-side phrase or LLM-classifier triggers.

## 3. Restart the gateway

```bash
hermes gateway restart
```

## 4. Tell the agent when to escalate

If you enabled `handover.tool`, the agent can call `trigger_handover`
mid-conversation. The tool description is intentionally generic, so
add a short "out of scope" section to your profile's `AGENTS.md`
spelling out which requests require a human owner.

Full docs: see `README.md` in this plugin directory, or
[github.com/pebble-tech/hermes-plugin-gateway-policy](https://github.com/pebble-tech/hermes-plugin-gateway-policy).

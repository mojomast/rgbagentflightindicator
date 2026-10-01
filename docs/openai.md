# Private ChatGPT and Codex plugin

The optional stdio MCP adapter exposes four tools: `panel_status`,
`session_start`, `session_set_state`, and `session_end`. It talks to the existing
panel on loopback. For ChatGPT behind NAT, OpenAI Secure MCP Tunnel supplies the
outbound connection; neither a router port nor a public daemon is needed.

This is a private testing integration. It does not include a hosted relay,
public distribution, a ChatGPT widget, or automatic conversation monitoring.
Everyone allowed to use the same tunnel can access that adapter's namespace;
this is not per-user authorization. Use one namespace per private tunnel and
restrict the tunnel to trusted testers.

## Install and test the local pieces

From the repository root, on Linux/macOS:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[openai]'
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python tools/scan_for_secrets.py
.venv/bin/python tools/check_portability.py
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.
The `openai` extra installs the official Python MCP SDK; the core installation
continues to work without it. Without the extra, MCP transport tests are skipped.

Start a panel in a terminal, using a dummy keyboard first:

```sh
.venv/bin/python -m rgi daemon --backend dummy --host 127.0.0.1 --port 8730
```

Leave that terminal running. When ready for hardware, restart it with an explicit
supported hardware backend. The MCP adapter never opens or probes hardware itself.

The adapter command is:

```sh
.venv/bin/python -m rgi mcp --namespace chatgpt-test
```

This speaks MCP over stdio and waits for a host; it is not an interactive shell.
Do not type prompts into it. Let tunnel-client or Codex launch it as a child.
Logs and errors use stderr; stdout is reserved for protocol messages.

The adapter reads the local panel credential from `RGI_TOKEN` or
`~/.config/rgi/token`. It never accepts credentials as tool arguments. The
default origin is `http://127.0.0.1:8730`; `--url` or `RGI_URL` may select another
loopback IP and port. URLs with credentials, remote hosts, paths or queries are
rejected, redirects are disabled, and inherited proxies are bypassed for local
API calls. Configure `--timeout` between 0.1 and 10 seconds (default 2).

## Connect ChatGPT through Secure MCP Tunnel

Follow the current [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
You need ChatGPT developer-mode access, a tunnel with the correct workspace
association, and a runtime API key whose principal has Tunnels **Read + Use**.
Creating a tunnel requires **Read + Manage**. Obtain these through
[Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels);
do not put keys in chat, this repository, the plugin ZIP, or command arguments.

Install the supported `tunnel-client` from Platform settings or its official
release. On macOS, the official installation path is:

```sh
brew install openai/tools/tunnel-client
```

Supply `CONTROL_PLANE_API_KEY` securely in the workstation process environment.
This authenticates the outbound tunnel; it is separate from the local RGI
credential. Do not run the client with an organization admin key.

Find the absolute Python executable path:

```sh
.venv/bin/python -c 'import sys; print(sys.executable)'
```

Replace the two placeholders below with that path and your tunnel ID. The quoted
Python path supports spaces; the installed editable package makes the command
independent of the tunnel client's working directory.

```sh
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile rgi-private \
  --tunnel-id '<your-tunnel-id>' \
  --mcp-command '"<absolute-python-path>" -m rgi mcp --namespace chatgpt-test'
tunnel-client doctor --profile rgi-private --explain
tunnel-client run --profile rgi-private
```

Use one active stdio client per tunnel ID. Keep `run` in a managed terminal for
the test, and stop it before starting a replacement. The daemon and the tunnel
client must both stay running; a sleeping workstation is unavailable. Check the
client's readiness diagnostics, not just whether its process exists.

In ChatGPT, enable developer mode in **Settings → Security and login**. At
[ChatGPT Plugins](https://chatgpt.com/plugins), create a connection, choose
**Tunnel**, and select the tunnel. Confirm all four RGI tools are discovered.
If it is not listed, check the ChatGPT workspace association and tunnel roles.
Developer-mode availability also depends on account/workspace policy.

## Package the private plugin with that connection

Copy the registered connection's technical ID from its browser URL; it starts
with `plugin_asdk_app_`. This ID is connection metadata, not an API key.

From the repository root, with a new output directory:

```sh
.venv/bin/python tools/package_openai_plugin.py \
  --app-id '<your-plugin_asdk_app_id>' \
  --output dist/chatgpt-private
```

This generates `plugins/rgi-panel`, a private local marketplace under
`.agents/plugins/marketplace.json`, and `rgi-panel.zip` containing the portable
manifest, the connection mapping, and the `rgi-panel` skill. The ChatGPT package
references your already registered tunnel connection; it does not attempt to
launch workstation Python from ChatGPT's cloud environment. No credentials are
copied. An existing output directory is refused; use a new directory for updates.

In a desktop host that supports local marketplaces, add the generated directory
as a local marketplace root, then install **RGB Agent Flight Indicator** from
**RGI Private Testing**. Where available, the catalog registration command is:

```sh
codex plugin marketplace add ./dist/chatgpt-private
```

Restart/refresh the desktop host and test in a new conversation. Local-marketplace
availability varies by surface. The browser developer-mode connection can test
the MCP tools directly even when local marketplace installation is unavailable.
See [OpenAI's private packaging instructions](https://developers.openai.com/plugins/build/plugins).
This ZIP is for private testing, not public submission.

## Local Codex package

For Codex on the keyboard workstation, skip the tunnel and registered connection:

```sh
.venv/bin/python tools/package_openai_plugin.py \
  --namespace codex-test \
  --output dist/codex-private
codex plugin marketplace add ./dist/codex-private
```

Install from the local marketplace in the supported host. This package contains
portable `mcp.json` pointing at the Python executable used for packaging. Rebuild
on each target machine or use `--python` to select its installed interpreter.
This local path does not make a cloud Codex task reach the workstation.

## Acceptance test in ChatGPT

Ask: “Use the RGB panel tools to test a lane: claim a fresh session, set working,
read status, set blocked, read status, set done, read status, then release it.”

Check that each state is returned by `panel_status`, the lane remains claimed
after `done`, and `session_end` frees it. A dummy backend tests software only;
confirm the physical colors separately on your supported keyboard.

Use a stable session ID per task. `session_start` is idempotent for that ID and
does not evict existing lanes. Completed sessions hold their lanes until release;
there is no adapter background expiry. `session_end` is idempotent. After a daemon
restart, reclaim with the same ID and preferred slot, then report the current
state. Adapter reads expose its own sessions, free-lane numbers, and device
descriptions, not other agents' labels, hosts or loose metadata.

On an unreachable panel, tools return an error. A timed-out write may have been
applied: inspect state before retrying. Keep the actual task independent of the
indicator. No tool can clear the whole panel, fetch arbitrary URLs, write files,
or execute shell commands.

## Disconnect

Release test sessions, stop the tunnel client you started, and remove/disable
the private connection and installed plugin in the host. Revoke the tunnel key
or tunnel access if no longer needed. Stop the dummy daemon you started. Removing
the plugin alone does not stop an independently running tunnel client or daemon.

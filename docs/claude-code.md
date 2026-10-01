# The Claude Code integration

Claude Code ships **command hooks**: at eight points in a session's life it runs a
shell command and hands it the event as JSON on stdin. `rgi hook claude-code` is
that command. Each session becomes one lane named `claude-code:<session_id>`,
green while the agent works, red while it waits on you, white when it lands — and
a `/resume` reuses the session id, so it lands on the same lamp again.

Nothing is installed inside Claude Code itself: eight short entries in
`settings.json` are the whole integration. The general contract every adapter
keeps is in [integrations.md](integrations.md).

## Installing it

The `rgi` command must be importable by the process Claude Code spawns. From the
checkout ([../README.md](../README.md)) that is:

```sh
git clone https://github.com/mojomast/rgbagentflightindicator
cd rgbagentflightindicator
pip install -e .
```

Then add the hooks to your user settings, `~/.claude/settings.json` (all
projects), or to a project's `.claude/settings.json` (that project only). Merge
this into the file rather than replacing it — `hooks` sits beside keys like
`permissions` you may already have:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "rgi hook claude-code" }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "rgi hook claude-code" }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          { "type": "command", "command": "rgi hook claude-code" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          { "type": "command", "command": "rgi hook claude-code" }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          { "type": "command", "command": "rgi hook claude-code" }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "rgi hook claude-code" }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          { "type": "command", "command": "rgi hook claude-code" }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          { "type": "command", "command": "rgi hook claude-code", "timeout": 10 }
        ]
      }
    ]
  }
}
```

Notes on the shape, from the [official reference](https://code.claude.com/docs/en/hooks):

- a **matcher** is optional; omitted, a group matches every occurrence of its
  event. Narrow one only if you want this panel to see fewer events
  (`"matcher": "Bash"` on `PostToolUse`, `"matcher": "permission_prompt"` on
  `Notification`), but then the lane misses the events you filtered out.
- `"timeout"` is in seconds. The `SessionEnd` hook gets 10 because Claude Code
  gives all `SessionEnd` hooks a shared 1.5-second budget; declaring a longer
  timeout raises it (up to 60), and a panel that takes a moment to answer should
  not be killed mid-report. The other events default to 600, which is plenty.
- if `rgi` is not on `PATH` where Claude Code runs (common for GUI launches),
  use exec form with the interpreter: `"command": "python", "args": ["-m",
  "rgi", "hook", "claude-code"]`, with the absolute path to the Python that has
  the package installed.

Claude Code picks settings changes up automatically; restart it if not. The
hooks add one short-lived process plus an HTTP call to each reporting event;
`RGI_URL` and `RGI_TOKEN` are inherited from Claude Code's environment, so set
them (or `~/.config/rgi/url` and `~/.config/rgi/token`) the same way as for any
other integration. `RGI_IDENT` names the machine's lanes.

Verify it works without Claude Code first:

```sh
echo '{"hook_event_name":"SessionStart","session_id":"hook-smoke","cwd":"/tmp/demo"}' \
  | rgi hook claude-code
rgi status        # a lane "claude-code:hook-smoke" on some lamp, working
```

Then type something into Claude Code, `/hooks` shows the eight entries, and the
lane appears on the panel.

## What each hook reports

| Claude Code event | what the lane does |
|---|---|
| `SessionStart` | adopts the lane (or claims a free lamp) and reports `working` |
| `UserPromptSubmit` | reports `working`; a new prompt resolves every earlier wait |
| `PreToolUse` | reports `working` |
| `PostToolUse` | reports `working`; resolves the wait for that tool, if one is open |
| `Notification` (`permission_prompt`, `idle_prompt`, `elicitation_dialog`, `elicitation_url_dialog`) | reports `blocked`, keyed by a stable request id |
| `Stop` | reports `done`; stopping resolves every open wait |
| `SubagentStop` | marks that subagent `done` under the lane's `children` (metadata) |
| `SessionEnd` | resolves every open wait, then releases the lamp |

A `Notification` for anything else (`auth_success`, `agent_completed`, ...) is
ignored, as is any event this integration did not install. Concurrent permission
prompts are counted by id: the lane stays red until the last one resolves, then
returns to the state before the wait. Because each hook is a separate process,
the open request ids live in `~/.config/rgi/integrations/claude-code-*.json`
(the same directory the Reporter uses for its event-ordering stamps); the file
holds ids and the scrubbed notification text only.

About exit codes, for reference: in Claude Code only **exit 2 blocks**, and only
on events that can block — `PreToolUse` (blocks the tool call),
`UserPromptSubmit` (rejects the prompt), `Stop` and `SubagentStop` (prevent
stopping). On `Notification`, `PostToolUse`, `SessionStart` and `SessionEnd`,
exit 2 cannot block. `rgi hook` always exits 0 and never writes to stdout, so
the panel can never change what the agent does.

## Removing it

- Delete the eight entries from `settings.json` (or the whole `hooks` object if
  this integration is all that is in it). `/hooks` then shows nothing.
- To keep the entries but silence them, set `"disableAllHooks": true` in a
  settings file, or run one session with
  `claude --settings '{"disableAllHooks": true}'`.
- Optionally delete `~/.config/rgi/integrations/claude-code-*.json` (only wait
  state; do it when no Claude Code session is running).
- The lane itself is released by the next `SessionEnd`, or by `rgi status`
  showing it and letting the daemon age it out as usual.

## Troubleshooting

| symptom | likely cause | fix |
|---|---|---|
| no lane, ever | `rgi` not found by the hook process, or the hooks are not loaded | run the smoke command above; check `/hooks`; use the absolute-interpreter exec form |
| lane appears but never leaves `working` | the `Stop` hook is missing, or the panel was unreachable at the moment | check `/hooks`; re-run the smoke command with `RGI_HOOK_DEBUG=1` |
| lane stays red after you answered | the matching `PostToolUse` hook is missing, or the tool was denied/interrupted before it ran | the next prompt, `Stop`, or `SessionEnd` clears it; install all eight events |
| lane stays red after a hard kill | no `SessionEnd` was delivered | it clears on the session's next event; delete the state file to clear it now |
| the wrong lamp looks busy | `RGI_URL`/`RGI_TOKEN` not visible to the hook process | hooks inherit Claude Code's environment; configure `~/.config/rgi/url` and `~/.config/rgi/token` |
| `[rgi] ...` lines on stderr | `RGI_HOOK_DEBUG`/`RGI_DEBUG` is set | unset it; the adapter is otherwise silent |
| a beat of latency on every tool call | one process and one HTTP call per event, by design | keep the panel on the same machine or LAN; tighten `timeout` on the panel side |
| `401` in debug output | wrong or missing token | write it to `~/.config/rgi/token` |

More panel-side symptoms are collected in [troubleshooting.md](troubleshooting.md).

## What is and is not reported

**Reported:** the lane (`claude-code:<session_id>`), its state
(`working`/`done`/`blocked`), a label taken from the project folder's name, the
open wait ids and the scrubbed notification text for them, subagent completions
as `children` metadata, and the machine name (`RGI_IDENT`, then the hostname).

**Never reported:** prompts, transcripts, tool names or arguments, tool results,
file contents or paths, the model, and token/cost/context figures — the hook
payloads do not carry them, and `rgi hook` writes nothing to stdout. Only the
notification text that accompanies a permission/input wait is published, because
that is what the wait is about.

## Supported versions

- **Package**: `@anthropic-ai/claude-code` (the Claude Code CLI and its command
  hooks). Hook packages from other vendors are not involved; the integration
  imports only this repository.
- **Verified against**: `2.1.286`, the npm release current when this page was
  written (`npm view @anthropic-ai/claude-code version`). The event names, JSON
  input fields, matcher rules, and exit-code table used above were checked
  against the official hooks reference, <https://code.claude.com/docs/en/hooks>,
  which documents 2.1.x: `session_id`, `hook_event_name`, `cwd`, `tool_name`,
  `tool_use_id`, `notification_type`, `message`, `title`, `agent_id`,
  `agent_type`, and `agent_transcript_path` are all read from that schema.
- **How it was verified**: `tests/test_hooks_claude.py` drives `hook()` with
  payloads shaped exactly like the reference's examples against the repository's
  mock panel. This is **simulated, not run against the real runtime** — no
  Claude Code process was installed or driven while building the integration.
- **Older releases**: `notification_type` and `SubagentStop`'s `agent_id` are
  absent from old 1.x payloads. The adapter handles that: without a type, an
  input wait is recognised from the message text; without an `agent_id`, the
  subagent gets a deterministic synthetic child id. There is no version check
  and no minimum enforced.

Limitations, honestly:

- simulated, not run against the real runtime (repeated because it matters);
- the `blocked` state begins when Claude Code sends the `Notification` — about
  six seconds after an unanswered permission prompt — not the instant the
  prompt appears;
- `Notification` payloads do not carry the tool-call id, so a wait is matched to
  its `PostToolUse` by the tool name in the message. Parallel waits that do not
  name their tool resolve oldest-first, one per completed tool; a wait resolved
  by another path stays red until the next prompt, `Stop`, or `SessionEnd`;
- a wait with no resolving event (crash, no `SessionEnd`) can stay red until the
  session's next event; the state file is per session and safe to delete;
- `done` stays on the panel until the next event or `SessionEnd`; there is no
  timer, because one process per event cannot hold one;
- only `SubagentStop` is reported — an in-flight subagent never appears; the
  completion is metadata and never claims a lamp;
- every installed event costs a process spawn and an HTTP call (`PreToolUse`
  and `PostToolUse` on every tool call); a dead panel can add up to ~2 seconds
  per call, which is why the `SessionEnd` example raises its timeout;
- notifications that are not permission/input waits, and hook events not listed
  above, are ignored; no token, cost, context, or model data is reported;
- two hook processes firing at the same instant for one session can race on the
  wait file; events within a session are effectively serial, so this is
  theoretical.

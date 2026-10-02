# Generic ingest

`rgi/ingest.py` is one pure normalizer that turns native hook and webhook
payloads into canonical actions the daemon can apply. It is the shared path
behind a future `POST /ingest` endpoint: instead of one Python adapter per
harness, each harness contributes a small mapping table and this module does
the translation.

```python
from rgi.ingest import sources, normalize

sources()
# ("amazon-q", "claude-code", "codex", "continue", "copilot", "cursor",
#  "devin", "gemini-cli", "generic", "github", "gitlab", "jenkins")

normalize("claude-code", {
    "hook_event_name": "Notification",
    "session_id": "abc-123",
    "notification_type": "permission_prompt",
    "tool_name": "Bash",
    "tool_use_id": "toolu_01ABC",
})
# [Action(op="attention", session="claude-code:abc-123", state="blocked", ...)]
```

The module is:

* **pure** — no threads, sockets, files, or daemon imports; the only import
  from the project is `rgi.report.canonical_state`, which owns the five state
  names;
* **stdlib only** and safe to import anywhere;
* **total** — an unknown source, an unknown event, a malformed body, or a
  body without a usable session id yields `[]`; `normalize()` never raises;
* **content-free** — only ids, names, counts and statuses are read. Prompts,
  transcripts, tool arguments, and tool results are never looked at, so there
  is nothing to scrub at this layer. (The daemon and `rgi/report.py` scrub
  everything they publish.)

## The `Action` shape

```python
@dataclass(frozen=True)
class Action:
    op: str                    # begin|state|info|end|attention|activity|snapshot
    session: str               # canonical "<namespace>:<session id>"
    agent: str = ""
    label: str | None = None
    state: str | None = None
    info: dict = field(default_factory=dict)
    request: str | None = None # wait/approval id for attention ops
    slot: int | None = None
    meta: dict = field(default_factory=dict)
```

| field | meaning |
|---|---|
| `op` | what to do; see the table below |
| `session` | the lane key. Hook sources use `<source>:<native id>`; CI uses `ci:<repo>:<pipeline>:<branch>`; `generic` keeps an already-keyed `sessionID` or prefixes it with `agent` |
| `agent` | the namespace to claim the lane under (or the CI provider) |
| `label` | a human-readable lane name; set on `begin` (project folder, check name, job) |
| `state` | one of `idle`, `working`, `blocked`, `done`, `error` — always produced via `rgi.report.canonical_state` |
| `info` | detail to merge one level, the same shape `POST /session/info` accepts |
| `request` | the wait/approval id on an `attention` action |
| `slot` | a preferred lamp, when the payload asks for one |
| `meta` | transport and identity side-data; see below |

### Applying an action

| op | daemon behavior |
|---|---|
| `begin` | adopt the lane if `session` exists, else claim one (honor `slot` only from the free list, never evict). Pass `agent`, `label`, `slot`; `meta["host"]`/`meta["ident"]` are the claim's `host`/`ident`. Idempotent. |
| `state` | report the transition; repeated states are no-ops and out-of-order `meta["at"]` may be dropped, exactly as `rgi/report.py` does. While any wait is open the lane stays `blocked`. |
| `info` | merge `info` into the lane's detail. |
| `end` | release the lane; idempotent. |
| `attention` | `meta["open"]=True` adds `request` to the pending set (lane reads `blocked`). `meta["open"]=False` removes: `request` names the wait; `request=None` + `meta["all"]=True` clears every wait; `request=None` + `meta["match"]="<tool>"` resolves the oldest wait matching that tool; `request=None` alone resolves the one wait this event ends. Waits are counted by id, never by a boolean. |
| `activity` | replace `info["running"]` with the list (`[{"tool": ..., "id": ...}]`). The accompanying `state` action carries the transition. |
| `snapshot` | apply `state` and merge `info` as a full snapshot; `meta["lease_s"]` is the requested lease. |

`meta` keys this module writes:

| key | on | meaning |
|---|---|---|
| `event` | hooks, CI | the native event/status word, for logs |
| `at` | when the payload carries a timestamp | epoch seconds (seconds, milliseconds, or ISO 8601 are accepted) |
| `delivery` | CI, `generic` CloudEvents | dedup key; the daemon should ignore a repeated `(source, delivery)` |
| `host`, `ident` | `generic` begin | lane identity passed to the claim |
| `open`, `all`, `match` | `attention` | wait protocol, above |
| `lease_s` | `snapshot` | lease seconds |

## Hook sources

Claude Code, Codex, Continue, and Cursor's Claude-compatible hooks share one
event table. Event names match case-insensitively and ignore `_`/`-`, so
`PreToolUse`, `preToolUse`, and `pre_tool_use` are the same row. A wait is
opened by `Notification`/`PermissionRequest`/`Elicitation` and closed by the
next event that resolves it.

| native event | actions |
|---|---|
| `SessionStart` | `begin`, attention close-all, `state working` |
| `UserPromptSubmit` | attention close-all, `state working` |
| `PreToolUse` | `state working`, `activity` |
| `PostToolUse`, `PostToolUseFailure` | attention close (by `tool_use_id`, else tool match), `state working`, `activity` |
| `PermissionRequest` | `attention` open, action `permission` |
| `Notification` | open only for `permission_prompt`, `idle_prompt`, `elicitation_dialog`, `elicitation_url_dialog`, `agent_needs_input`; any other notification yields `[]` |
| `Elicitation` | `attention` open, action `elicitation` |
| `Stop` | attention close-all, `state done` |
| `StopFailure`, `Interrupt` | attention close-all, `state error` (a cancel maps to `error`, as in `rgi/report.py`) |
| `SubagentStart` / `SubagentStop` | `info {"children": [...]}` state `working` / `done` — children are metadata, never their own lamp |
| `PreCompact`, `PostCompact` | `state working` |
| `SessionEnd` | attention close-all, `end` |
| anything else | `[]` |

An attention wait id comes from `tool_use_id`, `request_id`, `notification_id`,
`elicitation_id`, `tool_call_id`/`call_id`, or `id` (including inside Gemini's
`details`). When none exists, a synthetic id is a SHA-1 of
`(namespace, lane, kind, tool, event timestamp)` — never message text — so a
retried delivery is the same wait. The `info` of an open wait carries
`action` (`permission`, `input`, `elicitation`) and the tool name when known;
no message text is ever forwarded.

### Claude Code (`claude-code`)

The table above. Session is `claude-code:<session_id>`. Notification types are
the structured `notification_type` values only; prose is never parsed.

### Codex (`codex`)

The Claude table plus `PermissionRequest` and `Interrupt`, with `session_id`
also accepted as `thread_id`. Codex sends PascalCase names with snake_case
fields; both `PermissionRequest` and `permission_request` work.

### Cursor (`cursor`)

The Claude table plus Cursor's native events. Session is
`cursor:<session_id|conversation_id>`.

| native event | action |
|---|---|
| `sessionStart`, `sessionEnd` | the `SessionStart`/`SessionEnd` rows |
| `beforeSubmitPrompt` | attention close-all, `state working` |
| `preToolUse` / `postToolUse` / `postToolUseFailure` | the tool rows |
| `beforeShellExecution`, `afterShellExecution`, `beforeMCPExecution`, `afterMCPExecution`, `beforeReadFile`, `afterFileEdit`, `afterAgentResponse`, `afterAgentThought`, `preCompact` | `state working` |
| `subagentStart` / `subagentStop` | child metadata rows |
| `stop` | `status` `completed` → `done`, `aborted`/`error` → `error`, plus attention close-all |
| `workspaceOpen`, tab hooks | `[]` (not agent sessions) |

### Copilot (`copilot`)

The Claude table with `agentStop` → `Stop`, `userPromptSubmitted` /
`userPromptTransformed` → `UserPromptSubmit`, and `errorOccurred` → attention
close-all, `state error`.

Copilot emits two dialects: camelCase (`sessionId`, `toolName`, `toolArgs`,
`stopReason`) and PascalCase with snake_case fields (`session_id`, `tool_name`,
`timestamp`). Both are accepted. A camelCase payload that omits the event name
is recognised by shape — a tool result means `postToolUse`, a tool error means
`postToolUseFailure`, and so on.

**Session derivation.** When no `sessionId`/`session_id`/`conversation_id` is
present, the lane is derived:
`copilot:auto-<sha1(namespace + cwd + gpid/pid)[:12]>`. The pid comes from
`gpid`, `pid`, or `ppid` in the payload, then from `X-RGI-Pid`, `X-RGI-Gpid`,
`X-Process-Id`, or `X-Parent-Pid`. Same directory plus same agent process is
the same lane; a different process is a different lane.

### Continue (`continue`)

Claude Code-compatible event names and fields; session is
`continue:<session_id>`. Continue's extra events (`ConfigChange`,
`TeammateIdle`, `TaskCompleted`, `WorktreeCreate`, `WorktreeRemove`) are not
mapped and yield `[]`.

### Gemini CLI (`gemini-cli`)

Session is `gemini-cli:<session_id>`; `timestamp` feeds `meta["at"]`.

| native event | actions |
|---|---|
| `SessionStart` | `begin`, attention close-all, `state idle` |
| `BeforeAgent` | attention close-all, `state working` |
| `BeforeTool`, `AfterTool` | attention close-all, `state working`, `activity` |
| `Notification` with `notification_type: ToolPermission` | `attention` open, action `permission`, tool from `details.toolName`/`details.title` |
| `PreCompress` | `state working` |
| `AfterAgent` | attention close-all, `state done` |
| `SessionEnd` | attention close-all, `end` |
| any other notification | `[]` |

Gemini's approval notification usually carries no id, so the synthetic id is
used; `details.id`, `request_id`, and the documented id fields are honored when
a version sends one. Unlike the process-local adapter, the lane label is the
project folder or `gemini-cli`.

### Devin Desktop / Windsurf Cascade (`devin`)

Session is `devin:<trajectory_id>`. `windsurf`, `cascade`, and `devin-desktop`
are accepted aliases.

| native event (`agent_action_name`) | actions |
|---|---|
| `pre_user_prompt` | attention close-all, `state working` |
| `pre_read_code`, `post_read_code`, `pre_write_code`, `post_write_code`, `pre_run_command`, `post_run_command`, `pre_mcp_tool_use`, `post_mcp_tool_use`, `post_setup_worktree` | `state working` |
| `post_cascade_response`, `post_cascade_response_with_transcript` | attention close-all, `state done` |

`tool_info` is never read; the transcript path and response body are ignored.

### Amazon Q Developer CLI (`amazon-q`)

The Claude table plus `agentSpawn` → `begin`, attention close-all,
`state working`. `userPromptSubmit`, `preToolUse`, `postToolUse`, and `stop`
follow the shared rows.

**Amazon Q sends no session id anywhere.** If a future version adds one it is
used; otherwise the lane is derived exactly like Copilot's:

```
amazon-q:auto-<sha1(namespace + cwd + gpid/pid)[:12]>
```

`gpid`/`pid`/`ppid` come from the payload or the process-id headers above. The
pid is the agent process, not the short-lived hook process, so every event of
one session lands on one lane. With no pid available, the working directory is
the whole basis, and two Amazon Q sessions in the same directory share a lane —
that limitation is deliberate and visible rather than guessed around.

## Generic (`generic`)

The panel's own session payload:

```json
{"agent": "hermes", "sessionID": "job-123", "label": "nightly sync",
 "state": "running", "host": "kimi", "ident": "hermes-3", "info": {"repo": "nightly"}}
```

becomes `begin` (agent, label, slot, `meta.host`/`meta.ident`), then `state`
when `state`/`status` maps, then `info`. A `sessionID` already containing a
colon is treated as the canonical key; otherwise it is prefixed with `agent`.

The source also accepts the CloudEvents-shaped envelope from the ingestion
research: `type` of `dev.rgi.session.begin|state|info|end|attention|activity|
snapshot`, `dev.rgi.attention.open|close`, `subject` as the lane, `id` as the
delivery id, `time` as `meta.at`, and the payload under `data`. A top-level
`snapshot` object, or `op: "snapshot"` with `lease_s`, produces a `snapshot`
action. `state: "cancel"` maps to `error`.

## CI providers

One status vocabulary, three payload dialects. `queued`, `pending`,
`created`, `requested`, `scheduled` → `idle`; `in_progress`, `running`,
`started`, `preparing`, `building` → `working`; `success`, `succeeded`,
`passed`, `neutral` → `done`; `failure`, `failed`, `error`, `timed_out`,
`unstable` → `error`; `action_required`, `manual`, `waiting`,
`waiting_for_resource` → `blocked`; `cancelled`, `canceled`, `skipped`,
`aborted`, `not_built` → `end`. A state action is preceded by an idempotent
`begin`, followed by an `info` action describing the run, and `end` replaces
the state action for terminal cancellations.

The lane is `ci:<repo>:<pipeline-or-workflow>:<branch-or-pr>`, and the
delivery id is `meta["delivery"]`; the daemon should drop a second event with
the same `(source, delivery)` pair.

### GitHub (`github`)

`check_run` and `workflow_run` webhook bodies. `action: requested_action` and
`conclusion: action_required` are both `blocked`.

* lane: `ci:<repository.full_name>:<check or workflow name>:<pr-N | head_branch | run-id>`
* delivery: `X-GitHub-Delivery`, else `check_run:<id>:<conclusion | action | status>`
* `meta["event"]` is the webhook action; `meta["at"]` comes from the run's timestamps

### GitLab (`gitlab`)

Pipeline hooks (`object_kind: "pipeline"`; other hooks such as Job Hook are
ignored).

* lane: `ci:<project.path_with_namespace>:pipeline-<id>:<mr-N | ref>`
* delivery: `X-Gitlab-Event-UUID`, else `pipeline:<id>:<status>`
* `meta["event"]` is `X-Gitlab-Event` (for example `Pipeline Hook`)

### Jenkins (`jenkins`)

Notification-plugin JSON (`{"name", "url", "build": {...}}`); a top-level JSON
array is treated as a batch and normalized item by item.

* phase `QUEUED` → `idle`; `STARTED` → `working`; `DELETED` → `end`; otherwise
  the build `status` decides (`SUCCESS`, `FAILURE`, `ABORTED`, `NOT_BUILT`, …)
* lane: `ci:<job name>:build-<number>:<BRANCH_NAME | job>`
* delivery: `build.full_url` (the Jenkins build URL)

## Adding a source

Add a profile to `_PROFILES` (event/state table, session fields, label
fallback), or a CI function, register it in `_NORMALIZERS` and `_SOURCES`, add
a recorded payload plus its expected actions under `tests/fixtures/ingest/`,
and run:

```
python -m unittest tests.test_ingest
```

The fixture test walks every case in every manifest and compares the exact
actions, so a new mapping row is a JSON pair, not a new test method.

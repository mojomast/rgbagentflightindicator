# Zoo Code CLI runs

[Zoo Code](https://zoocode.dev) is a community fork of
[Roo Code](https://github.com/Zoo-Code-Org/Zoo-Code): a VS Code extension that
also ships a command line agent. This integration covers the **command line**,
which is the only place Zoo Code offers a supported machine interface.

That is worth being precise about, because it is a limitation and not a
preference. Checked against Zoo Code **3.84.0**, the extension has **no
lifecycle-hook system** to attach to: every `hook` in that tree is a React hook,
and the Claude-compatible hooks work that exists upstream (Roo Code
[#10785](https://github.com/RooCodeInc/Roo-Code/pull/10785)) was exploratory and
never shipped. So conversations inside the editor are **not** covered here, and
this page makes no claim about them. What *is* covered is the headless run:

```sh
roo --print --output-format stream-json "fix the failing tests"
```

which writes newline-delimited JSON on stdout — transport `roo-cli-stream`,
schema version 1. Headless runs are the ones that deserve a lamp anyway: nobody
is sitting in that window.

## Usage

The wrapper is a drop-in for the command it wraps. It adds `--print
--output-format stream-json`, mirrors the stream onto one lane, echoes the
stream to stdout as it arrives, and returns the CLI's own exit code:

```sh
python -m rgi.integrations.zoo_cli "fix the failing tests"
python -m rgi.integrations.zoo_cli "audit the deps" -- --model gpt-5
python -m rgi.integrations.zoo_cli --keep-result 60 "run the suite"   # hold the lamp
```

Flags before `--` configure the reporter (`--url`, `--token`, `--ident`,
`--session-id`, `--bin`, `--no-echo`, `--keep-result SECONDS`,
`--require-approval`); flags after `--` go to Zoo Code untouched. The CLI is
found as `$RGI_ZOO_BIN`, then `roo` on `PATH`, then `zoo` — the fork still ships
the upstream package name and binary, so both spellings are tried. If none is
found the wrapper exits **127** with that list, rather than pretending to work.

Already have the stream? Feed it in yourself — no subprocess, no Zoo Code:

```python
from rgi.integrations.zoo_cli import ZooStreamReporter

with ZooStreamReporter(session_id=task_uuid, label="audit the deps") as lane:
    for line in open("zoo.ndjson", encoding="utf-8"):
        lane.feed_line(line)
```

Long-lived harnesses can use Zoo Code's own stdin protocol
(`--stdin-prompt-stream`, `--signal-only-exit`) and keep one `ZooStreamReporter`
for the life of the process.

## What it reports

| stream event | lane |
|---|---|
| `system` / `init` | claims the lane (named after the prompt), then *in flight* |
| `user`, `assistant`, `thinking` | *in flight* |
| `assistant` with `subtype: "followup"` | **needs a human** |
| a following `user` event | back to *in flight* — somebody answered |
| `tool_use` | *in flight*, plus the tool name and a one-line step |
| `tool_result` | *in flight*; a non-zero `exitCode` is noted, not reddened |
| `queue` | publishes the queue depth |
| `error` | *needs attention*, and the next event puts it back to work |
| `result` with `success: true` | *complete* |
| `result` with `success: false` | *needs attention*, with the reason |

Cost rides along on `result` as `{totalCost, inputTokens, outputTokens,
cacheWrites, cacheReads}` and is published in the vocabulary the sidebar already
reads: `tokens.input`, `tokens.output`, `tokens.cost`, `tokens.cache_read`.

The lane's id is, in order: the `--session-id` you passed (or Zoo Code's own
`--session-id` / `--create-with-session-id`, picked out of the command line), the
`taskId` Zoo puts on its events, or a generated one. Resuming a task therefore
lands on the same lane again, and a second run of the same session id joins it
rather than taking a second lamp.

The lane is released when the run finishes, like every other integration's
`close()`. Pass `--keep-result 60` to hold it for a minute first, so a human can
read the outcome off the keyboard.

## When the lamp blinks red

Only one event blocks by default: `assistant` carrying `subtype: "followup"`,
which is Zoo Code's `ask_followup_question` — the agent is asking the human
something. A following `user` event resolves it, and the end of a run clears any
question nobody answered.

A `tool_use` with no `tool_result` is deliberately **not** a wait. Zoo Code's
CLI decides its own behaviour with `nonInteractive: !requireApproval`
(`apps/cli/src/commands/cli/run.ts`), so in a plain `--print` run it answers its
own asks — and the stream cannot tell an auto-answered tool from one a human is
being asked about. Guessing wrong leaves a lamp blinking forever, which is worse
than a late blink. If you run the CLI the other way, with its own
`-a/--require-approval`, then that pairing really is a pending decision, so pass
`--require-approval` here too and it will be reported as one:

```sh
python -m rgi.integrations.zoo_cli --require-approval "refactor this" -- -a
```

Waits are counted by id, so two questions at once both survive, and one answer
clears both.

## Troubleshooting

- **nothing on the panel, no error** — the run finished before its first event,
  so no lamp was ever claimed; check `python -m rgi doctor` for the panel and the
  token first.
- **`ignored a line that was not JSON`** in the log — the CLI printed prose,
  which means it was not in `stream-json` mode. The wrapper forces the flag; if
  you see this, something else is writing to that stdout.
- **exit 127** — no Zoo Code CLI on this machine. Install `@roo-code/cli` or set
  `RGI_ZOO_BIN`.
- **a lane that stayed blocked** — a `followup` question went unanswered. The
  wrapper resolves it when the run ends; if the process was killed outright, run
  `rgi lane-map` to see which lanes are held and `rgi status` to read them.

## Supported versions

- **Zoo Code extension:** **3.84.0** (GitHub release, published 2026-09-26,
  <https://github.com/Zoo-Code-Org/Zoo-Code/releases/tag/v3.84.0>). Checked
  2026-10-01 to establish that no lifecycle hooks exist to attach to; every
  `hook` path in that tree is a React hook under `apps/cli/src/ui/hooks/`,
  `webview-ui/src/hooks/`, or `src/core/`, and the upstream hooks work
  ([Roo Code #10785](https://github.com/RooCodeInc/Roo-Code/pull/10785)) is a
  WIP pull request, not a release.
- **Zoo Code CLI:** **0.1.17**, package `@roo-code/cli`, binary `roo`
  (`apps/cli/package.json`, `version` and `bin`). The fork has not renamed the
  CLI package as of 3.84.0. The wrapper also tries a binary named `zoo` in case
  that changes.
- **The event contract was read from the released sources, not guessed**:
  the event types, cost shape, output formats and stdin protocol in
  [`packages/types/src/cli.ts`](https://github.com/Zoo-Code-Org/Zoo-Code/blob/main/packages/types/src/cli.ts);
  the NDJSON schema in
  [`apps/cli/src/types/json-events.ts`](https://github.com/Zoo-Code-Org/Zoo-Code/blob/main/apps/cli/src/types/json-events.ts);
  the mapping from internal messages to those events in
  [`apps/cli/src/agent/json-event-emitter.ts`](https://github.com/Zoo-Code-Org/Zoo-Code/blob/main/apps/cli/src/agent/json-event-emitter.ts);
  and the `nonInteractive` decision in
  [`apps/cli/src/commands/cli/run.ts`](https://github.com/Zoo-Code-Org/Zoo-Code/blob/main/apps/cli/src/commands/cli/run.ts).
  Retrieved 2026-10-01 from the fork's `main` branch, which is one commit past
  the 3.84.0 release.
- **Simulated, not run against the real runtime.** The tests replay a recorded
  NDJSON stream and a fake process against the mock panel
  ([`tests/test_zoo_cli.py`](../tests/test_zoo_cli.py)); no live Zoo Code CLI and
  no real provider was exercised.

Limitations:

- Headless runs only. Conversations inside the VS Code extension have no
  supported hook point at 3.84.0, so there is no watcher for them and this
  integration does not pretend otherwise. Revisit when Zoo Code ships a hooks
  system — the adapter would then read the same events from a hook payload
  instead of a stream.
- `control` events (stdin command acknowledgements) and any event type newer than
  the list above are ignored: no report, never an error.
- `schemaVersion` is read but not enforced. An unknown protocol is logged once
  and the run continues.
- Only `followup` asks are treated as human waits, and only when the CLI is in
  its interactive modes (`--require-approval`, or the stdin stream). In a plain
  `--print` run Zoo Code answers its own questions, and the stream does not say
  otherwise.
- Tool *input* is summarised to one scrubbed line (`command`, `path`, `query`,
  …) and never forwarded whole: arguments can hold anything the agent read.
- Subagents take no lamp. Zoo Code's CLI does not expose them as events in this
  mode, so `children` stays empty rather than being guessed at.
- One lane per run: a second run with the same session id joins the lane already
  there.

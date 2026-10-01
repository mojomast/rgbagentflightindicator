"""Report a headless Zoo Code run to the panel, from the stream it prints.

Zoo Code (https://zoocode.dev) is a community fork of Roo Code: a VS Code
extension that also ships a command line agent. This adapter covers the CLI in
its non-interactive form, which is the only place Zoo Code offers a supported
machine interface:

    roo --print --output-format stream-json "fix the failing tests"

That writes newline-delimited JSON on stdout - transport ``roo-cli-stream``,
schema version 1 - with the event types Zoo declares in ``rooCliEventTypes``:
``system``, ``control``, ``queue``, ``assistant``, ``user``, ``tool_use``,
``tool_result``, ``thinking``, ``error``, ``result``. Cost rides along as
``{totalCost, inputTokens, outputTokens, cacheWrites, cacheReads}``.

Scope, stated honestly: there is no lifecycle-hook system in the extension to
attach to. Checked against Zoo Code 3.84.0 - every ``hook`` in that tree is a
React hook, and the Claude-compatible hooks work that exists upstream never
shipped - so conversations inside the editor are *not* covered here. Headless
runs are, and those are the ones worth a lamp: nobody is watching them.

The event mapping, and the reasoning behind it:

* ``system``/``init`` -> claim the lane, then ``working``;
* ``user``, ``assistant``, ``thinking``, ``tool_use`` -> ``working``;
* ``assistant`` carrying ``subtype: "followup"`` -> ``blocked``. That is Zoo's
  ``ask_followup_question``, and it is the one event that only appears when a
  human is genuinely wanted. A following ``user`` event resolves it;
* ``tool_result`` -> ``working``. A non-zero ``exitCode`` is ordinary work, so
  it never turns the lamp red - only a ``result`` with ``success: false`` does;
* ``error`` -> ``error(...)``, and the next event puts the lane back to work,
  because Zoo retries some of these itself and a recovered blip is not a failure;
* ``result`` -> clears any open wait, then ``done()`` or ``error()``.

A ``tool_use`` with no ``tool_result`` is deliberately *not* a human wait by
default. The CLI decides its own behaviour with
``nonInteractive: !requireApproval`` (``apps/cli/src/commands/cli/run.ts``), so
in a plain ``--print`` run Zoo answers its own asks and the stream cannot tell
an auto-answered tool from one a human is being asked about - guessing wrong
means a permanently blinking lamp. Pass ``block_on_tool_use=True`` when you run
with ``-a/--require-approval``, where a tool with no result really is a decision
somebody has to make.

Nothing here imports Zoo Code. :class:`ZooStreamReporter` folds in events from
any source - a pipe, a file, a test - and :func:`run_zoo` is the convenience
wrapper that spawns the CLI and echoes its stream. Only a missing CLI raises;
every reporting failure is swallowed, so a panel that is down costs the lamp,
not the run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Iterable

from ..report import Reporter, scrub

NAMESPACE = "zoo"
PROTOCOL = "roo-cli-stream"

# The event types Zoo declares in rooCliEventTypes.
EVENT_SYSTEM = "system"
EVENT_CONTROL = "control"
EVENT_QUEUE = "queue"
EVENT_ASSISTANT = "assistant"
EVENT_USER = "user"
EVENT_TOOL_USE = "tool_use"
EVENT_TOOL_RESULT = "tool_result"
EVENT_THINKING = "thinking"
EVENT_ERROR = "error"
EVENT_RESULT = "result"

SUBTYPE_INIT = "init"
SUBTYPE_FOLLOWUP = "followup"

# Tool input keys worth showing, in the order they are preferred.
_BRIEF_KEYS = ("command", "path", "file_path", "filePath", "query", "pattern",
               "url", "server", "name")


def session_id_in(argv: Iterable[str]) -> str:
    """Zoo's own resume id from a command line, so a resumed run reuses its lane.

    ``--session-id`` resumes a task and ``--create-with-session-id`` names a new
    one; both are UUIDs Zoo keeps across runs, which is exactly the stability
    our lane identity wants.
    """
    items = [str(a) for a in argv]
    for flag in ("--session-id", "--create-with-session-id"):
        for index, item in enumerate(items):
            if item == flag and index + 1 < len(items):
                return items[index + 1].strip()
            if item.startswith(flag + "="):
                return item.split("=", 1)[1].strip()
    return ""


def _brief(value: Any) -> str:
    """One short, safe line about what a tool was asked to do."""
    if not isinstance(value, dict):
        return ""
    for key in _BRIEF_KEYS:
        found = value.get(key)
        if isinstance(found, (str, int, float)) and str(found).strip():
            return scrub(found, 100)
    return ""


def _cost_fields(cost: Any) -> dict:
    """Zoo's cost payload in the vocabulary the sidebar already reads."""
    if not isinstance(cost, dict):
        return {}
    fields: dict = {}
    for key, value in (("input", cost.get("inputTokens")),
                       ("output", cost.get("outputTokens")),
                       ("cost", cost.get("totalCost")),
                       ("cache_read", cost.get("cacheReads"))):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fields[key] = value
    return fields


class ZooStreamReporter:
    """Mirror one Zoo Code CLI stream onto one panel lane.

    Feed it a line (:meth:`feed_line`) or an event (:meth:`handle`) at a time.
    The lane is claimed on the first event, so a stream that never says anything
    never takes a lamp. The lane's id is, in order: the session id you passed,
    the ``taskId`` Zoo puts on its own events, or a generated one.

    :meth:`close` releases the lane and is safe twice, and safe on a dead panel.
    """

    def __init__(self, *, session_id: str | None = None, label: str | None = None,
                 reporter: Reporter | None = None, namespace: str = NAMESPACE,
                 block_on_tool_use: bool = False,
                 log: Callable[[str], None] | None = None, **reporter_kwargs):
        self.session_id = str(session_id or "").strip()
        self.label = label
        self.namespace = namespace
        self.block_on_tool_use = bool(block_on_tool_use)
        self._log = log
        self._kwargs = reporter_kwargs
        self._reporter = reporter
        self._lock = threading.RLock()
        self._waits: set[str] = set()
        self._tools: dict[str, str] = {}
        self._seq = 0

    # -- diagnostics -------------------------------------------------------
    def _note(self, message: str) -> None:
        if self._log is None:
            return
        try:
            self._log(f"zoo: {message}")
        except Exception:                        # noqa: BLE001 - never raise
            pass

    def _next(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    @property
    def reporter(self) -> Reporter | None:
        return self._reporter

    @property
    def pending(self) -> list[str]:
        """The waits this lane is currently blocked on."""
        return sorted(self._waits)

    # -- lifecycle ---------------------------------------------------------
    def _ensure(self, event: Any = None) -> Reporter | None:
        if self._reporter is not None:
            return self._reporter
        with self._lock:
            if self._reporter is not None:
                return self._reporter
            sid = self.session_id
            if not sid and isinstance(event, dict):
                sid = str(event.get("taskId") or "").strip()
            sid = sid or uuid.uuid4().hex[:12]
            try:
                self._reporter = Reporter(self.namespace, sid, label=self.label,
                                          log=self._log, **self._kwargs)
            except Exception as exc:             # noqa: BLE001 - never raise
                self._note(f"could not build a reporter: {exc}")
                return None
            return self._reporter

    def close(self) -> None:
        """Release the lane. Never raises, safe to call twice."""
        try:
            if self._reporter is not None:
                self._reporter.close()
        except Exception as exc:                 # noqa: BLE001 - never raise
            self._note(f"close failed: {exc}")

    def __enter__(self) -> "ZooStreamReporter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- input -------------------------------------------------------------
    def feed_line(self, line: str) -> bool:
        """Fold in one line of NDJSON. Anything that is not JSON is ignored."""
        text = (line or "").strip()
        if not text:
            return False
        try:
            event = json.loads(text)
        except ValueError:
            self._note("ignored a line that was not JSON")
            return False
        return self.handle(event)

    def handle(self, event: Any) -> bool:
        """Fold in one event. True when the lane was touched."""
        if not isinstance(event, dict):
            return False
        kind = str(event.get("type") or "").strip()
        if not kind:
            return False
        subtype = str(event.get("subtype") or "").strip()
        try:
            if kind == EVENT_SYSTEM:
                return self._started(event, subtype)
            if kind in (EVENT_USER, EVENT_ASSISTANT, EVENT_THINKING):
                return self._talking(event, kind, subtype)
            if kind == EVENT_TOOL_USE:
                return self._tool_started(event)
            if kind == EVENT_TOOL_RESULT:
                return self._tool_finished(event)
            if kind == EVENT_QUEUE:
                return self._queued(event)
            if kind == EVENT_ERROR:
                return self._failed(event)
            if kind == EVENT_RESULT:
                return self._finished(event)
            # control events, and anything newer than this file
            return False
        except Exception as exc:                 # noqa: BLE001 - never raise
            self._note(f"ignored a {kind} event: {exc}")
            return False

    # -- event mapping -----------------------------------------------------
    def _started(self, event: dict, subtype: str) -> bool:
        reporter = self._ensure(event)
        if reporter is None:
            return False
        protocol = str(event.get("protocol") or "")
        if protocol and protocol != PROTOCOL:
            self._note(f"unexpected stream protocol {protocol!r}; carrying on")
        claimed = reporter.start()
        self._publish_cost(reporter, event)
        reporter.working()
        return claimed

    def _talking(self, event: dict, kind: str, subtype: str) -> bool:
        reporter = self._ensure(event)
        if reporter is None:
            return False
        if kind == EVENT_ASSISTANT and subtype == SUBTYPE_FOLLOWUP:
            return self._ask(reporter, event)
        if kind == EVENT_USER:
            return self._answered(reporter)
        reporter.working()
        return True

    def _ask(self, reporter: Reporter, event: dict) -> bool:
        rid = f"followup-{self._next()}"
        message = scrub(event.get("content") or "", 120)
        with self._lock:
            self._waits.add(rid)
        return reporter.blocked(request=rid, action="question",
                                message=message or "Zoo Code is asking a question")

    def _answered(self, reporter: Reporter) -> bool:
        """A human replied: whatever we were waiting for is over."""
        with self._lock:
            waits = sorted(self._waits)
            self._waits.clear()
        for rid in waits:
            reporter.resolve(rid)
        reporter.working()
        return True

    def _tool_started(self, event: dict) -> bool:
        reporter = self._ensure(event)
        if reporter is None:
            return False
        use = event.get("tool_use")
        use = use if isinstance(use, dict) else {}
        name = str(use.get("name") or event.get("subtype") or "tool").strip() or "tool"
        tid = str(event.get("id") or "").strip() or f"{name}-{self._next()}"
        brief = _brief(use.get("input"))
        with self._lock:
            self._tools[tid] = name
            waiting = self.block_on_tool_use
            if waiting:
                self._waits.add(tid)
        reporter.working()
        fields = {"tool": name}
        if brief:
            fields["step"] = brief
        reporter.info(fields)
        if waiting:
            reporter.blocked(request=tid, action=name,
                             message=brief or f"waiting for approval: {name}")
        return True

    def _tool_finished(self, event: dict) -> bool:
        reporter = self._ensure(event)
        if reporter is None:
            return False
        tid = str(event.get("id") or "").strip()
        result = event.get("tool_result")
        result = result if isinstance(result, dict) else {}
        with self._lock:
            self._tools.pop(tid, None)
            was_waiting = tid in self._waits
            self._waits.discard(tid)
        if was_waiting:
            reporter.resolve(tid)
        reporter.working()
        exit_code = result.get("exitCode")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code:
            # a command that failed is still work; say so without reddening
            reporter.info({"last_exit": exit_code})
        return True

    def _queued(self, event: dict) -> bool:
        reporter = self._ensure(event)
        if reporter is None:
            return False
        depth = event.get("queueDepth")
        if not isinstance(depth, int) or isinstance(depth, bool):
            return False
        return reporter.info({"queued": depth})

    def _failed(self, event: dict) -> bool:
        reporter = self._ensure(event)
        if reporter is None:
            return False
        message = scrub(event.get("content") or "", 120)
        return reporter.error(message or "the Zoo Code CLI reported an error")

    def _finished(self, event: dict) -> bool:
        reporter = self._ensure(event)
        if reporter is None:
            return False
        self._publish_cost(reporter, event)
        # The run is over, so nothing is pending any more - including a followup
        # question the CLI never got an answer to.
        self._answered(reporter)
        if event.get("success") is False:
            message = scrub(event.get("content") or "", 120)
            return reporter.error(message or "the Zoo Code run failed")
        return reporter.done()

    def _publish_cost(self, reporter: Reporter, event: dict) -> bool:
        fields = _cost_fields(event.get("cost"))
        return reporter.info({"tokens": fields}) if fields else False


def find_binary(explicit: str | None = None) -> str:
    """The Zoo Code CLI: an explicit path, ``$RGI_ZOO_BIN``, ``roo``, then ``zoo``.

    The fork still ships the upstream package name and binary (``@roo-code/cli``,
    ``roo``) at 3.84.0, so both spellings are tried.
    """
    for candidate in (explicit, os.environ.get("RGI_ZOO_BIN"), "roo", "zoo"):
        if not candidate:
            continue
        found = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if found and os.path.exists(found):
            return found
    raise FileNotFoundError(
        "the Zoo Code CLI was not found - install @roo-code/cli (binary `roo`) "
        "or point RGI_ZOO_BIN at it")


def run_zoo(args: Iterable[str] | None = None, *, prompt: str | None = None,
            session_id: str | None = None, binary: str | None = None,
            block_on_tool_use: bool = False, echo: bool = True,
            keep_result: float = 0.0, **reporter_kwargs) -> int:
    """Run the Zoo Code CLI with a lamp on the panel, and return its exit code.

    The stream is echoed to stdout as it arrives, so this is a drop-in wrapper:
    ``run_zoo(["--model", "gpt-5"], prompt="fix the tests")`` behaves like the
    command it wraps. ``keep_result`` holds the finished lane for that many
    seconds before releasing it, for a human who wants to read the lamp.

    Only a missing CLI raises. Every reporting problem is swallowed by the
    reporter, and the run continues without an indicator.
    """
    exe = find_binary(binary)
    argv = [exe, "--print", "--output-format", "stream-json"]
    argv.extend(str(a) for a in (args or []))
    if prompt:
        argv.append(str(prompt))
    lane = ZooStreamReporter(session_id=session_id or session_id_in(argv),
                             label=prompt, block_on_tool_use=block_on_tool_use,
                             **reporter_kwargs)
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   bufsize=1)
    except OSError as exc:
        lane.close()
        raise FileNotFoundError(f"could not start {exe}: {exc}") from exc
    try:
        assert process.stdout is not None
        for line in process.stdout:
            lane.feed_line(line)
            if echo:
                sys.stdout.write(line)
                sys.stdout.flush()
    except KeyboardInterrupt:
        process.terminate()
        lane.handle({"type": EVENT_ERROR, "content": "interrupted"})
        return 130
    finally:
        code = process.wait()
        if keep_result and keep_result > 0:
            time.sleep(float(keep_result))
        lane.close()
    return code


def main(argv: list[str] | None = None) -> int:
    """``python -m rgi.integrations.zoo_cli "task" -- --model gpt-5``"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m rgi.integrations.zoo_cli",
        description="Run the Zoo Code CLI with a lamp on the panel.")
    parser.add_argument("prompt", nargs="?", help="what to ask Zoo Code")
    parser.add_argument("--url", help="panel url (default $RGI_URL or the config)")
    parser.add_argument("--token", help="panel token (default $RGI_TOKEN or the config)")
    parser.add_argument("--ident", help="lane name (default the machine's name)")
    parser.add_argument("--session-id", help="stable id for this lane")
    parser.add_argument("--bin", help="path to the Zoo Code CLI")
    parser.add_argument("--no-echo", action="store_true",
                        help="do not echo the CLI's stream to stdout")
    parser.add_argument("--keep-result", type=float, default=0.0, metavar="SECONDS",
                        help="hold the finished lane before releasing it")
    parser.add_argument("--require-approval", action="store_true",
                        help="also treat a tool_use with no result as a human wait "
                             "(use with the CLI's own -a/--require-approval)")
    known, rest = parser.parse_known_args(argv)
    try:
        return run_zoo(rest, prompt=known.prompt, session_id=known.session_id,
                       binary=known.bin, echo=not known.no_echo,
                       keep_result=known.keep_result,
                       block_on_tool_use=known.require_approval,
                       url=known.url, token=known.token, ident=known.ident)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 127


if __name__ == "__main__":                     # pragma: no cover - entry point
    sys.exit(main())

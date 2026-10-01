"""Watch OpenCode sessions and report them as lanes.

    rgi watch --url http://127.0.0.1:8730 --token <token>
    python rgi-watch.py --url http://panel:8730      # standalone, as published

OpenCode's HTTP API is reached through its own CLI (``opencode api get ...``),
which handles authentication, so no credentials are read or stored here.

    session running            -> working   (green)
    session stopped            -> done      (blinks white, then holds)
    permission prompt pending  -> blocked   (blinks red)

A lane is only given up when the session has been idle for ``--stale`` seconds,
or when another session needs the lamp and it is the least recently used. That
is deliberate: the point of the indicator is that a number keeps meaning the same
session. (An early version freed the lamp 45 s after every turn and never sent
``working`` again for a session that had already landed - so a lane lit once and
then vanished. Both are fixed here.)

Everything it decides is appended to ``~/.config/rgi/watcher.log``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8730"
ACTIVE_POLL = 1.5        # how often to ask OpenCode what is running
SESSION_POLL = 20.0      # how often to refresh titles and last-used times
DONE_GRACE = 3.0         # a stop this short is not a finished turn
STALE_DEFAULT = 2 * 3600
HEARTBEAT = 10.0          # seconds between reports for a lane that is busy

STATE_DIR = os.path.join(os.path.expanduser("~"), ".config", "rgi")
LOG_PATH = os.path.join(STATE_DIR, "watcher.log")
LOCK_PATH = os.path.join(STATE_DIR, "watcher.lock")
URL_PATH = os.path.join(STATE_DIR, "url")
TOKEN_PATH = os.path.join(STATE_DIR, "token")
NAME_PATH = os.path.join(STATE_DIR, "name")


def _pid_alive(pid: int) -> bool:
    """Is that process still there?

    Deliberately not os.kill(pid, 0): on Windows os.kill does not send a signal,
    it calls TerminateProcess, so the "is it alive?" probe would kill the very
    watcher it is asking about. Ask the OS instead.
    """
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_single_instance(path: str = LOCK_PATH) -> str | None:
    """Claim the machine's watcher slot, or report who already holds it.

    Two watchers on one machine fight: each claims its own lanes for the same
    sessions, so the panel ends up with duplicates and the keyboard with lamps
    nobody can explain. Better to refuse the second one loudly.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                holder = fh.read().strip()
            pid = int(holder.split()[0])
        except (OSError, ValueError):
            pid = None
        if pid and _pid_alive(pid):
            return holder
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()} (since {time.strftime('%Y-%m-%d %H:%M:%S')})")
    return None


def release_instance(path: str = LOCK_PATH) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def say(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def opencode_exe() -> str:
    """Resolve the CLI once.

    Never pass shell=True with an argument list: on POSIX that runs only the
    first element, so ``opencode api get <path>`` becomes a bare ``opencode``
    that hangs. Windows hides that because cmd.exe joins the list - and it needs
    this lookup because the npm shim is ``opencode.cmd``, which CreateProcess
    will not find by bare name.
    """
    return shutil.which("opencode") or "opencode"


OPENCODE = opencode_exe()


SHELL_TOOLS = {"bash", "shell", "terminal", "sh", "cmd", "powershell", "pwsh", "exec"}


class Watcher:
    def __init__(self, url: str = DEFAULT_URL, token: str | None = None,
                 include_subagents: bool = False, stale: float = STALE_DEFAULT,
                 ident: str | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self.include_subagents = include_subagents
        self.stale = stale
        self.host = socket.gethostname()
        # every lane this watcher claims is named after the machine
        self.ident = resolve_ident(ident)
        self.bound: dict[str, dict] = {}
        self.parents: dict[str, str | None] = {}
        self._classified: set[str] = set()   # sessions we have asked metadata about
        self.titles: dict[str, str] = {}
        self.updated: dict[str, float] = {}
        self.records: dict[str, dict] = {}          # sessionID -> record from /api/session
        self.children: dict[str, list[str]] = {}     # parentID -> child session ids
        self.last_meta = 0.0
        self.warned = False
        self.last_error = ""
        self._repo_cache: dict[str, dict] = {}
        self._context_cache: dict[str, tuple[float, dict]] = {}
        self._info_sent: dict[str, str] = {}
        self._info_time: dict[str, float] = {}
        self._tools_cache: dict[str, tuple[float, list]] = {}
        self._running: set[str] = set()
        self._attention_detail: dict[str, dict] = {}

    # -- HTTP -------------------------------------------------------------
    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-LED-Token"] = self.token
        return h

    def get(self, path: str) -> dict | None:
        try:
            req = urllib.request.Request(self.url + path, headers=self.headers())
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read() or b"{}")
        except Exception:
            return None

    def post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.url + path, data=body,
                                     headers=self.headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            self.last_error = f"HTTP {exc.code}"
            if exc.code == 401:
                self.last_error = "HTTP 401 - the panel wants X-LED-Token"
            return {"error": self.last_error}
        except Exception as exc:
            self.last_error = str(exc)
            return {"error": self.last_error}

    # -- OpenCode ---------------------------------------------------------
    def cli(self, path: str, timeout: float = 25.0):
        try:
            proc = subprocess.run([OPENCODE, "api", "get", path],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout)
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if proc.returncode != 0:
            return None, (proc.stderr or "").strip()[:200] or f"exit {proc.returncode}"
        text = (proc.stdout or "").strip()
        if not text:
            return None, "empty response"
        try:
            return json.loads(text), None
        except Exception as exc:
            return None, f"bad json: {exc}"

    # -- lanes ------------------------------------------------------------
    def bind(self, sid: str, want: int | None = None) -> dict | None:
        # Named after the machine, like every other lane this watcher claims. An
        # agent the human gave a personal name sets it with /session/info, and a
        # re-claim never overwrites that: claim() returns early for a live lane.
        payload = {"agent": "opencode", "sessionID": sid, "ident": self.ident,
                   "label": self.titles.get(sid) or sid, "host": self.host}
        if want is not None:
            payload["slot"] = want
        res = self.post("/session/start", payload)
        if "slot" not in res and want is not None:
            payload.pop("slot", None)          # someone took it; take any free lamp
            res = self.post("/session/start", payload)
        if "slot" not in res:
            return None
        return {"slot": res["slot"], "key": res.get("key"), "state": None,
                "stopped_at": None, "done_at": None, "ignored": False}

    def set_state(self, sid: str, state: str) -> bool:
        res = self.post("/session/state", {"sessionID": sid, "state": state})
        if res.get("ok"):
            return True
        info = self.bound.get(sid)
        fresh = self.bind(sid, want=info.get("slot") if info else None)
        if fresh is None:
            return False
        if info is not None:
            info.update(slot=fresh["slot"], key=fresh["key"])
        return bool(self.post("/session/state", {"sessionID": sid, "state": state}).get("ok"))

    def land(self, sid: str, reason: str) -> None:
        info = self.bound[sid]
        state = info.get("state")
        if state in ("working", "blocked", None):
            info["stopped_at"] = time.monotonic()
            info["state"] = "stopping"
            return
        if state != "stopping":
            return
        if time.monotonic() - (info["stopped_at"] or 0) < DONE_GRACE:
            return
        if self.set_state(sid, "done"):
            info.update(state="done", done_at=time.monotonic(), stopped_at=None)
            say(f"[land] {sid[-12:]} -> {info.get('key')}  ({reason})")

    def free(self, sid: str, reason: str) -> None:
        self.post("/session/end", {"sessionID": sid})
        info = self.bound.pop(sid, None)
        self._info_sent.pop(sid, None)
        self._context_cache.pop(sid, None)
        if info:
            say(f"[free] {sid[-12:]} -> {info.get('key')}  ({reason})")

    def refresh_metadata(self) -> None:
        data, err = self.cli("/api/session")
        if data is None:
            if not self.warned:
                say(f"[warn] could not read /api/session: {err}")
            return
        self.children = {}
        for s in data.get("data", []):
            sid = s.get("id")
            if not sid:
                continue
            self.parents[sid] = s.get("parentID")
            self.titles[sid] = s.get("title")
            self.updated[sid] = ((s.get("time") or {}).get("updated") or 0) / 1000.0
            self.records[sid] = s
            parent = s.get("parentID")
            if parent:
                self.children.setdefault(parent, []).append(sid)

    def classify_new(self, running: set[str]) -> None:
        """Ask for metadata before binding a session we have never seen.

        A subagent can start between two metadata refreshes. Without this the
        watcher would see an unknown active session, bind it as a root, and the
        child would keep a lamp until it went stale - a lamp taken from real
        work by something that is only metadata under its parent's lane. One
        extra ask covers a whole burst of new sessions, and each session is
        asked about once.
        """
        fresh = [sid for sid in running
                 if sid not in self.bound and sid not in self.parents
                 and sid not in self._classified]
        if not fresh:
            return
        if len(self._classified) > 4096:
            self._classified.clear()
        self._classified.update(fresh)
        self.refresh_metadata()

    def release_subagents(self) -> None:
        """A lane that turns out to be a subagent gives its lamp back.

        This is the other half of the race: a child bound before its parentage
        was known, or one whose record only appeared later. On the next pass the
        metadata says what it is, and the lamp is returned.
        """
        if self.include_subagents:
            return
        for sid, info in list(self.bound.items()):
            if info.get("ignored") or not self.parents.get(sid):
                continue
            self.free(sid, "subagent - children are metadata, not lanes")

    # -- the detail the sidebar shows when a lane is uncollapsed ----------
    def _repo(self, directory: str | None) -> dict:
        """Repository name and branch for a session's working directory.

        Cached per directory: this reads two small files, and sessions in the
        same project share the answer.
        """
        if not directory:
            return {}
        if directory in self._repo_cache:
            return self._repo_cache[directory]

        info: dict = {"directory": directory}
        try:
            config = open(os.path.join(directory, ".git", "config"),
                          encoding="utf-8", errors="replace").read()
            match = None
            for line in config.splitlines():
                line = line.strip()
                if line.startswith("url = "):
                    match = line[6:].strip()
                    break
            if match:
                name = match.rstrip("/").split("/")[-1]
                info["repo"] = name[:-4] if name.endswith(".git") else name
                info["remote"] = match
        except OSError:
            pass
        info.setdefault("repo", os.path.basename(directory.rstrip("\\/")) or directory)
        try:
            head = open(os.path.join(directory, ".git", "HEAD"),
                        encoding="utf-8", errors="replace").read().strip()
            if head.startswith("ref: refs/heads/"):
                info["branch"] = head[len("ref: refs/heads/"):]
        except OSError:
            pass
        self._repo_cache[directory] = info
        return info

    def _context(self, sid: str, now: float) -> dict:
        """Compaction count, and a percentage if a context limit is configured.

        OpenCode exposes no context limit anywhere in its API, so a percentage
        needs RGI_CONTEXT_LIMIT (in tokens). Without it we report the pressure
        signal we do have: how many times this session has been compacted.
        """
        cached = self._context_cache.get(sid)
        if cached and now - cached[0] < 60:
            return cached[1]

        context: dict = {}
        data, _ = self.cli(f"/api/session/{sid}/context", timeout=15)
        if isinstance(data, dict):
            events = data.get("data") or []
            if isinstance(events, list):
                # this endpoint lists context entries; only count the compactions,
                # and report the total separately so neither number misleads
                context["entries"] = len(events)
                context["compactions"] = sum(
                    1 for e in events if isinstance(e, dict) and e.get("type") == "compaction")
        limit = os.environ.get("RGI_CONTEXT_LIMIT")
        if limit and limit.isdigit():
            context["limit"] = int(limit)
            used = self._last_prompt_tokens(sid)
            if used:
                context["used"] = used
                context["percent"] = round(100.0 * used / int(limit), 1)
        self._context_cache[sid] = (now, context)
        return context

    def _last_prompt_tokens(self, sid: str) -> int | None:
        """Prompt size of the most recent assistant turn, if we can read it."""
        data, _ = self.cli(f"/api/session/{sid}/message", timeout=20)
        if not isinstance(data, dict):
            return None
        messages = data.get("data") or []
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            if message.get("type") != "assistant":
                continue
            tokens = message.get("tokens") or {}
            try:
                return int(tokens.get("input") or 0) or None
            except (TypeError, ValueError):
                return None
        return None

    def _running_tools(self, sid: str, now: float) -> list[dict]:
        """Shell commands this session is running right now.

        Only shells, not every tool: a lane reading a file is busy, while a lane
        running a command is busy in a way you may want to interrupt. The last
        assistant message carries its tool parts with their state, so the command
        line is available while it runs.
        """
        cached = self._tools_cache.get(sid)
        if cached and now - cached[0] < 5:
            return cached[1]

        running: list[dict] = []
        data, _ = self.cli(f"/api/session/{sid}/message", timeout=20)
        if isinstance(data, dict):
            messages = data.get("data") or []
            for message in reversed(messages):
                if message.get("type") != "assistant":
                    continue
                for part in message.get("content") or []:
                    if not isinstance(part, dict) or part.get("type") != "tool":
                        continue
                    state = part.get("state") or {}
                    if state.get("status") not in ("running", "pending"):
                        continue
                    tool = str(part.get("name") or "").lower()
                    if tool not in SHELL_TOOLS:
                        continue
                    detail = tool
                    raw = state.get("input") or {}
                    if isinstance(raw, dict):
                        for key in ("command", "cmd", "filePath", "path", "pattern", "url"):
                            if raw.get(key):
                                detail = str(raw[key])
                                break
                    running.append({
                        "tool": part.get("name") or "tool",
                        "detail": " ".join(str(detail).split())[:60],
                    })
                break                       # only the latest assistant message
        self._tools_cache[sid] = (now, running)
        return running

    def _lane_info(self, sid: str, now: float) -> dict:
        record = self.records.get(sid) or {}
        directory = (record.get("location") or {}).get("directory")
        tokens = record.get("tokens") or {}

        info: dict = {}
        info.update(self._repo(directory))
        info["tokens"] = {
            "input": tokens.get("input"),
            "output": tokens.get("output"),
            "reasoning": tokens.get("reasoning"),
            "cache_read": (tokens.get("cache") or {}).get("read"),
            "cost": record.get("cost"),
        }
        info["context"] = self._context(sid, now)

        kids = []
        for child in self.children.get(sid, []):
            # "in flight" means OpenCode says it is running *and* it has been heard
            # from recently: the active list alone is not enough, and listing
            # twenty finished subagents is exactly the noise this avoids
            if child not in self._running:
                continue
            last = self.updated.get(child) or 0.0
            if last and now - last > 600:
                continue
            if len(kids) >= 6:
                break
            child_record = self.records.get(child) or {}
            kids.append({
                "id": child,
                "label": (child_record.get("title") or child)[:40],
                "state": "working",
                "tokens": ((child_record.get("tokens") or {}).get("output")),
            })
        # always send these, even when empty: the panel merges detail rather than
        # replacing it, so an omitted key leaves the previous value on screen -
        # which is how twenty-seven finished subagents stayed listed.
        info["children"] = kids
        info["running"] = self._running_tools(sid, now)

        blocked = self._attention_detail.get(sid)
        if blocked:
            info["blocked_on"] = blocked
        return info

    def push_info(self, running: set[str]) -> None:
        """Send lane detail to the panel.

        Posts when it changes, and otherwise at least every HEARTBEAT seconds
        while a lane is busy - the panel measures "idle" from the last report, so
        without a heartbeat a working lane and a stuck one look identical.
        """
        now = time.time()
        self._running = running
        for sid, lane in list(self.bound.items()):
            if lane.get("ignored"):
                continue
            try:
                info = self._lane_info(sid, now)
            except Exception as exc:                 # detail must never break lanes
                say(f"[warn] could not build detail for {sid[-12:]}: {exc}")
                continue
            signature = json.dumps(info, sort_keys=True)
            fresh = self._info_sent.get(sid) == signature
            last = self._info_time.get(sid, 0.0)
            busy = lane.get("state") in ("working", "blocked", "stopping")
            if fresh and not (busy and now - last > HEARTBEAT):
                continue
            self._info_sent[sid] = signature
            self._info_time[sid] = now
            self.post("/session/info", {"sessionID": sid, "info": info})

    def attention(self) -> set[str]:
        """Sessions with an unanswered permission prompt, and what they wait on."""
        data, _ = self.cli("/api/permission/request")
        if data is None:
            return set()
        ids: set[str] = set()
        details: dict[str, dict] = {}
        for item in data.get("data") or []:
            sid = item.get("sessionID")
            if not sid:
                continue
            ids.add(sid)
            details[sid] = {
                "action": item.get("action"),
                "resources": (item.get("resources") or [])[:3],
                "message": (item.get("message") or "")[:120] or None,
            }
        self._attention_detail = details
        return ids

    def stale_sessions(self) -> list[str]:
        now = time.time()
        out = []
        for sid, info in self.bound.items():
            if info.get("ignored") or info.get("state") in ("working", "stopping", "blocked"):
                continue
            last = self.updated.get(sid) or 0.0
            idle = (now - last) if last else (time.monotonic() - (info.get("done_at") or time.monotonic()))
            if idle > self.stale:
                out.append((idle, sid))
        out.sort(reverse=True)
        return [sid for _, sid in out]

    def reconcile(self) -> None:
        status = self.get("/status")
        if status is None:
            return
        live = set((status.get("sessions") or {}).keys())
        for sid, info in list(self.bound.items()):
            if info.get("ignored") or info.get("slot") is None or sid in live:
                continue
            fresh = self.bind(sid, want=info.get("slot"))
            if fresh is None:
                say(f"[warn] could not re-claim {sid[-12:]}: the panel refused a lamp")
                continue
            if fresh["slot"] != info.get("slot"):
                say(f"[slot] {sid[-12:]} was {info.get('slot')}, now {fresh['slot']}")
            info.update(slot=fresh["slot"], key=fresh["key"])
            state = info.get("state") or "working"
            if state == "stopping":
                state = "working"
            if self.set_state(sid, state):
                info["state"] = state
                say(f"[again] {sid[-12:]} -> {info.get('key')}  ({state})")

    def run(self) -> None:
        holder = acquire_single_instance()
        if holder:
            say(f"another watcher already owns this machine's lanes: {holder}")
            say("not starting a second one - two would claim the same sessions twice.")
            say("stop that one first, or run `rgi doctor` to see what is installed.")
            return

        try:
            self._run()
        finally:
            release_instance()

    def _run(self) -> None:
        say(f"watching OpenCode; panel at {self.url}")
        say(f"subagents: {'included' if self.include_subagents else 'ignored'}"
            f"   lanes freed after {self.stale / 60:.0f} min idle")
        say(f"host reported as {self.host}; opencode CLI: {OPENCODE}")
        say(f"lanes are named {self.ident!r} (--ident, RGI_IDENT, "
            f"~/.config/rgi/name, else the hostname)")

        while True:
            if time.time() - self.last_meta > SESSION_POLL:
                self.refresh_metadata()
                self.reconcile()
                self.last_meta = time.time()

            active, err = self.cli("/api/session/active")
            if active is None:
                if not self.warned:
                    say(f"[warn] could not read /api/session/active: {err}")
                    self.warned = True
                time.sleep(ACTIVE_POLL)
                continue
            self.warned = False

            running = set((active.get("data") or {}).keys())
            # Classify before binding, and hand back any lane that turned out to
            # be a child: subagents are metadata, never lamps.
            self.classify_new(running)
            self.release_subagents()
            attention = self.attention()
            self.push_info(running)

            for sid in sorted(running):
                if sid in self.bound:
                    continue
                if self.parents.get(sid) and not self.include_subagents:
                    self.bound[sid] = {"slot": None, "key": None, "state": None,
                                       "ignored": True, "stopped_at": None, "done_at": None}
                    continue
                info = self.bind(sid)
                if info is None:
                    # never silent: a refused lane is usually a missing token or a
                    # full panel, and both are worth knowing about
                    say(f"[warn] could not claim a lane for {sid[-12:]} "
                        f"(panel refused it: {self.last_error or 'no free lanes'})")
                    continue
                self.bound[sid] = info
                if self.set_state(sid, "working"):
                    info["state"] = "working"
                    say(f"[bind] {sid[-12:]} -> {info.get('key')}  (in flight)")

            for sid, info in list(self.bound.items()):
                if info.get("ignored"):
                    if sid not in running:
                        self.bound.pop(sid, None)
                    continue

                if sid in attention:
                    if info.get("state") != "blocked" and self.set_state(sid, "blocked"):
                        info.update(state="blocked", stopped_at=None)
                        say(f"[hold] {sid[-12:]} -> {info.get('key')}  (needs you)")
                    continue

                if sid in running:
                    if info.get("state") != "working":
                        was = info.get("state")
                        if self.set_state(sid, "working"):
                            if was == "done":
                                say(f"[go]   {sid[-12:]} -> {info.get('key')}  (working again)")
                            elif was == "blocked":
                                say(f"[work] {sid[-12:]} -> {info.get('key')}  (unblocked)")
                            info["state"] = "working"
                    info["stopped_at"] = None
                    info["done_at"] = None
                    continue

                self.land(sid, "turn finished")

            for sid in self.stale_sessions():
                self.free(sid, f"idle > {self.stale / 60:.0f} min")

            time.sleep(ACTIVE_POLL)


def resolve_ident(explicit: str | None = None) -> str:
    """The name every lane from this machine gets: the machine's name.

    Order: ``--ident``, ``RGI_IDENT``, ``~/.config/rgi/name``, then the hostname
    (short form). The file exists so a human can name a machine once without
    editing anything, and the hostname is the default so lanes are never left
    labelled with the harness they happen to run in.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("RGI_IDENT")
    if env and env.strip():
        return env.strip()
    try:
        with open(NAME_PATH, encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    except OSError:
        pass
    return socket.gethostname().split(".")[0]


def _env_url() -> str:
    """The panel address, the same way the plugin and the client resolve it."""
    env = os.environ.get("RGI_URL") or os.environ.get("LEDD_URL")
    if env:
        return env.rstrip("/")
    try:
        with open(URL_PATH, encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value.rstrip("/")
    except OSError:
        pass
    return DEFAULT_URL


def _env_token() -> str | None:
    env = os.environ.get("RGI_TOKEN") or os.environ.get("LEDD_TOKEN")
    if env:
        return env
    try:
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            value = fh.read().strip()
        return value or None
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    """Run the watcher with no package around it.

    The panel publishes this file at /files/rgi-watch.py, so a machine can report
    its sessions with nothing but Python 3: no install, no checkout, no
    dependencies. That matters because the tokens, context, subagents and running
    shells a lane can show all come from here, and an old watcher only sends the
    lane state.
    """
    ap = argparse.ArgumentParser(description="report OpenCode sessions to the panel")
    ap.add_argument("--url", default=None,
                    help="panel address (default: RGI_URL, then ~/.config/rgi/url, "
                         "then " + DEFAULT_URL + ")")
    ap.add_argument("--token", default=None,
                    help="shared secret (default: RGI_TOKEN, then ~/.config/rgi/token)")
    ap.add_argument("--include-subagents", action="store_true")
    ap.add_argument("--stale", type=float, default=STALE_DEFAULT,
                    help="seconds of inactivity before a lamp is reused")
    ap.add_argument("--ident", default=None,
                    help="name every lane this watcher claims (default: RGI_IDENT, "
                         "then ~/.config/rgi/name, then the hostname)")
    args = ap.parse_args(argv)
    watcher = Watcher(
        url=args.url or _env_url(),
        token=args.token or _env_token(),
        include_subagents=args.include_subagents,
        stale=args.stale,
        ident=args.ident,
    )
    watcher.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

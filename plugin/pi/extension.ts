// Pi coding agent extension: one panel lane per Pi session.
//
// Verified against the released npm package @earendil-works/pi-coding-agent
// 0.99.2 (the published tarball's docs/extensions.md and
// dist/core/extensions/types.d.ts - not the default branch):
//
//   1. An extension is a default-exported factory `(pi: ExtensionAPI) => ...`.
//      Every event registration returns a function that unsubscribes exactly
//      that registration; the factory's return value is ignored by the loader.
//      Cleanup therefore hangs off `session_shutdown`, which fires for quit,
//      reload and session replacement.
//
//   2. `agent_start` fires when a run begins. `agent_end` fires when a
//      low-level run ends, but Pi may still auto-retry, auto-compact and
//      retry, or run queued follow-ups; only `agent_settled` means Pi will
//      not continue automatically. Completion is reported on `agent_settled`
//      alone.
//
//   3. 0.99.2 has no permission event. A permission check is an extension UI
//      dialog (`ctx.ui.confirm`/`select`, as examples/extensions/permission-gate.ts
//      uses), and every blocking dialog is bracketed by `ui_prompt_start` and
//      `ui_prompt_end` with kind "confirm" | "select" | "input" | "editor" |
//      "custom". Only confirm/select are real questions with a yes/no or pick
//      answer, so only those make the lane blocked; the lane resolves when the
//      prompt closes.
//
// The lane identity is `pi:<session id>`, named with the machine's hostname.
// Every panel call is best-effort with a short timeout: the panel is an
// indicator, never a dependency. Nothing here throws into Pi, and nothing
// prints the token.
//
// Install by copying this one file to ~/.pi/agent/extensions/ (user) or
// .pi/extensions/ (project), or load it for one run with
// `pi --extension <this file>`.

import { readFileSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const CONFIG_DIR = join(homedir(), ".config", "rgi");
const DEFAULT_URL = "http://127.0.0.1:8730";
// Short on purpose: a slow or black-holed panel must never delay Pi or the
// run. Every call is bounded; a failure is swallowed and the work goes on.
const TIMEOUT_MS = 1500;
const HEARTBEAT_MS = 30_000;
const MAX_INFO = 160;

// The same resolution order as rgi/config.py: environment, then the config
// file (a process that started before the environment changed still finds the
// panel), then localhost.
function resolveUrl(): string {
  const env = (process.env.RGI_URL ?? process.env.LEDD_URL ?? "").trim();
  if (env) return env.replace(/\/+$/, "");
  try {
    const file = readFileSync(join(CONFIG_DIR, "url"), "utf8").trim();
    if (file) return file.replace(/\/+$/, "");
  } catch {
    /* no file is the normal case on the panel's own machine */
  }
  return DEFAULT_URL;
}

function resolveToken(): string {
  const env = process.env.RGI_TOKEN ?? process.env.LEDD_TOKEN;
  if (env) return env;
  try {
    return readFileSync(join(CONFIG_DIR, "token"), "utf8").trim();
  } catch {
    return "";
  }
}

// One line, safe to publish: no secrets, no control characters, bounded. The
// patterns mirror rgi/report.py's scrub(). Nothing read here is printed.
const SECRETS: RegExp[] = [
  /\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}/g,
  /\bgh[pousr]_[A-Za-z0-9]{16,}/g,
  /\b(?:AKIA|ASIA)[A-Z0-9]{12,}/g,
  /(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}/gi,
  /(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+/gi,
];

function scrub(value: unknown, limit = MAX_INFO): string {
  let clean = String(value ?? "").replace(/\s+/g, " ").trim();
  for (const pattern of SECRETS) clean = clean.replace(pattern, "[redacted]");
  return clean.slice(0, limit);
}

// urllib.parse.quote(safe="") equivalent, so /session/<key> matches what the
// Python reporter asks for.
function quote(value: string): string {
  return encodeURIComponent(value).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
}

function signal(): AbortSignal | undefined {
  try {
    return AbortSignal.timeout(TIMEOUT_MS);
  } catch {
    return undefined;
  }
}

// A second factory call in one runtime must not register a second listener
// for any event. Pi re-imports the module on reload, so a fresh module gets a
// fresh factory; this guards only against an accidental double load.
let activeDispose: (() => void) | null = null;

export default function extension(pi: ExtensionAPI): void | (() => void) {
  if (activeDispose) return activeDispose;

  const url = resolveUrl();
  const token = resolveToken();
  const machine = hostname();
  const ident = (process.env.RGI_IDENT ?? "").trim() || machine;

  const state = {
    key: "",                                             // "pi:<session id>" the lane belongs to
    lane: null as { slot: number; key: string } | null,  // the claimed lamp
    intent: "idle" as string,                            // last non-blocked state
    sent: null as string | null,                         // what the panel last acknowledged
    online: false,
    lastInfo: "",
    pending: new Map<string, { action: string; message: string }>(),
    prompts: [] as Array<{ id: string; kind: string }>,
    seq: 0,
    heartbeat: null as ReturnType<typeof setInterval> | null,
  };

  const saidOnce = new Set<string>();
  const unsubscribers: Array<() => void> = [];
  let disposed = false;

  function debug(message: string): void {
    if (process.env.RGI_HOOK_DEBUG !== "1") return;
    try {
      process.stderr.write(`rgi pi: ${message}\n`);
    } catch {
      /* stderr must never break the extension either */
    }
  }

  function sayOnce(kind: string, message: string): void {
    if (saidOnce.has(kind)) return;
    saidOnce.add(kind);
    debug(`${kind}: ${message}`);
  }

  // -- the panel ---------------------------------------------------------
  // One HTTP call, fully guarded: a dead panel, a timeout, a non-JSON body
  // and a 401 all come back as null/false and are logged once at most.
  async function call(
    method: string,
    path: string,
    payload?: unknown,
  ): Promise<{ status: number; body: Record<string, any> } | null> {
    try {
      const headers: Record<string, string> = { Accept: "application/json" };
      if (token) headers["X-LED-Token"] = token;
      if (payload !== undefined) headers["Content-Type"] = "application/json";
      const res = await fetch(url + path, {
        method,
        headers,
        body: payload === undefined ? undefined : JSON.stringify(payload),
        signal: signal(),
      });
      state.online = res.status < 500;
      let body: Record<string, any> = {};
      try {
        body = (await res.json()) as Record<string, any>;
      } catch {
        body = {};
      }
      if (res.status === 401) {
        sayOnce("auth", "the panel rejected the token; check RGI_TOKEN or ~/.config/rgi/token");
      }
      return { status: res.status, body };
    } catch {
      state.online = false;
      sayOnce("offline", "the panel did not answer; carrying on without an indicator");
      return null;
    }
  }

  function sessionKey(ctx: ExtensionContext): string {
    try {
      const id = ctx.sessionManager.getSessionId();
      return id ? `pi:${id}` : "";
    } catch {
      return "";
    }
  }

  // Adopt the current session key, releasing any lane left over from a
  // previous session in the same process. Never evicts someone else's lane.
  async function adopt(key: string): Promise<boolean> {
    if (!key) return false;
    if (state.key === key) return true;
    const previous = state.key;
    state.key = key;
    state.lane = null;
    state.sent = null;
    state.lastInfo = "";
    state.pending.clear();
    state.prompts.length = 0;
    stopHeartbeat();
    if (previous) await endKey(previous);
    return true;
  }

  async function endKey(key: string): Promise<void> {
    if (!key) return;
    const res = await call("POST", "/session/end", { sessionID: key });
    if (!res || res.status === 200 || res.status === 404) debug(`released ${key}`);
  }

  // Adopt the lane for this session, or claim the first free lamp. Claiming
  // by number from the free list means a new session can never take a lamp
  // that belongs to somebody's blocked lane.
  async function startLane(): Promise<boolean> {
    if (!state.key) return false;
    const found = await call("GET", `/session/${quote(state.key)}`);
    if (found && found.status === 200 && found.body.found) {
      state.lane = { slot: Number(found.body.slot ?? 0), key: String(found.body.key ?? "") };
      state.sent = found.body.state ? String(found.body.state) : null;
      return true;
    }
    const status = await call("GET", "/status");
    if (!status || status.status !== 200) return false;
    const free = (Array.isArray(status.body.free) ? status.body.free : [])
      .map((value: unknown) => Number(value))
      .filter((value: number) => Number.isFinite(value));
    if (!free.length) {
      sayOnce("no_lane", "no free lamps; reporting without an indicator rather than evicting someone");
      return false;
    }
    const started = await call("POST", "/session/start", {
      sessionID: state.key,
      label: scrub(ident, 60),
      agent: "pi",
      ident,
      host: machine,
      slot: free[0],
    });
    if (started && started.status === 200 && started.body.slot !== undefined) {
      state.lane = { slot: Number(started.body.slot), key: String(started.body.key ?? "") };
      state.sent = null;
      return true;
    }
    if (started && started.status === 409) {
      sayOnce("lane_taken", "the free lamp was taken; reporting without an indicator");
    }
    return false;
  }

  async function pushState(next: string): Promise<boolean> {
    if (!state.lane && !(await startLane())) return false;
    let res = await call("POST", "/session/state", { sessionID: state.key, state: next });
    if (res && res.status === 404) {
      // the daemon restarted, or the lane was freed while we waited
      state.lane = null;
      if (!(await startLane())) return false;
      res = await call("POST", "/session/state", { sessionID: state.key, state: next });
    }
    if (res && res.status === 200 && res.body.ok) {
      state.sent = next;
      return true;
    }
    return false;
  }

  async function setState(next: string, force = false): Promise<boolean> {
    if (next !== "blocked") state.intent = next;
    const effective = state.pending.size ? "blocked" : state.intent;
    if (!force && effective === state.sent && state.online) return true;
    return pushState(effective);
  }

  async function info(fields: Record<string, unknown>): Promise<boolean> {
    const clean: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(fields)) {
      clean[key] = typeof value === "string" ? scrub(value) : value;
    }
    const signature = JSON.stringify(clean);
    if (signature === state.lastInfo && state.online) return true;
    if (!state.lane && !(await startLane())) return false;
    let res = await call("POST", "/session/info", { sessionID: state.key, info: clean });
    if (res && res.status === 404) {
      state.lane = null;
      if (!(await startLane())) return false;
      res = await call("POST", "/session/info", { sessionID: state.key, info: clean });
    }
    if (res && res.status === 200) {
      state.lastInfo = signature;
      return true;
    }
    return false;
  }

  // -- attention ---------------------------------------------------------
  // Waits are counted by id, like rgi/report.py: several prompts can be open
  // without cancelling each other, and the lane stays blocked until the last
  // one resolves. Only a dialog that is genuinely waiting on a human gets an
  // id.
  function attention(): Record<string, unknown> {
    const ids = [...state.pending.keys()].sort();
    if (!ids.length) return { pending_requests: [], blocked_on: null };
    const first = state.pending.get(ids[0]) ?? { action: "", message: "" };
    return {
      pending_requests: ids,
      blocked_on: {
        action: first.action || "input",
        message: first.message || `${ids.length} request(s) pending`,
        resources: ids.slice(0, 3),
      },
    };
  }

  async function blocked(id: string, action: string, message: string): Promise<void> {
    state.pending.set(scrub(id, 64) || "attention", {
      action: scrub(action, 40),
      message: scrub(message, MAX_INFO),
    });
    await info(attention());
    await setState("blocked", true);
  }

  async function resolve(id: string): Promise<void> {
    state.pending.delete(scrub(id, 64) || "attention");
    await info(attention());
    await setState(state.intent, true);
  }

  // -- timers ------------------------------------------------------------
  function startHeartbeat(): void {
    if (state.heartbeat) return;
    state.heartbeat = setInterval(() => void heartbeat(), HEARTBEAT_MS);
  }

  async function heartbeat(): Promise<void> {
    if (!state.key) return;
    if (!state.lane) {
      await startLane();
      return;
    }
    const res = await call("POST", "/session/info", {
      sessionID: state.key,
      heartbeat: Math.round(Date.now() / 1000),
    });
    if (res && res.status === 404) state.lane = null;
  }

  function stopHeartbeat(): void {
    if (!state.heartbeat) return;
    clearInterval(state.heartbeat);
    state.heartbeat = null;
  }

  async function endLane(): Promise<void> {
    stopHeartbeat();
    state.pending.clear();
    state.prompts.length = 0;
    if (!state.key) return;
    const key = state.key;
    state.lane = null;
    state.sent = null;
    state.lastInfo = "";
    await endKey(key);
  }

  // -- event plumbing ----------------------------------------------------
  // Handlers are queued so two rapid events (start then settle, prompt start
  // then end) are sent in the order they happened; one call in flight at a
  // time also keeps the lane's state on the panel consistent with the intent.
  let queue: Promise<void> = Promise.resolve();

  function run(name: string, task: () => Promise<void>): Promise<void> {
    queue = queue.then(() => task()).catch((err) => {
      sayOnce(`handler:${name}`, `${name}: ${scrub(err, 120)}`);
    });
    return queue;
  }

  // Every public entry point is guarded: sync throws and rejections are
  // swallowed here and logged once at most, never into Pi's dispatcher.
  function guard<T>(
    name: string,
    fn: (event: T, ctx: ExtensionContext) => void | Promise<void>,
  ): (event: T, ctx: ExtensionContext) => void | Promise<void> {
    return (event: T, ctx: ExtensionContext) => {
      try {
        return fn(event, ctx);
      } catch (err) {
        sayOnce(`handler:${name}`, `${name}: ${scrub(err, 120)}`);
        return undefined;
      }
    };
  }

  function track(off: () => void): void {
    unsubscribers.push(off);
  }

  try {
    // Panel calls happen in the background: Pi's dispatcher never waits on
    // the indicator, and `run()` keeps their order and swallows failures.
    // `session_shutdown` is the exception - it is awaited so the lane is
    // released before the process goes away, and even then it is bounded by
    // the short timeout.
    track(pi.on("agent_start", guard("agent_start", (_event, ctx) => {
      void run("agent_start", async () => {
        if (!(await adopt(sessionKey(ctx)))) return;
        // claim the lane first: this is where a free lamp is adopted or taken
        await setState("working");
        startHeartbeat();
      });
    })));

    track(pi.on("agent_settled", guard("agent_settled", (_event, ctx) => {
      void run("agent_settled", async () => {
        if (!(await adopt(sessionKey(ctx)))) return;
        // A settled run cannot still be waiting on a human. If an end event
        // was lost, drop the wait rather than leave the lane red forever.
        if (state.pending.size || state.prompts.length) {
          state.pending.clear();
          state.prompts.length = 0;
          await info(attention());
        }
        stopHeartbeat();
        await setState("done", true);
      });
    })));

    track(pi.on("ui_prompt_start", guard("ui_prompt_start", (event, ctx) => {
      void run("ui_prompt_start", async () => {
        const kind = String(event?.kind ?? "");
        // confirm/select are the approval mechanism; input/editor/custom are
        // not decisions and are left alone.
        if (kind !== "confirm" && kind !== "select") return;
        if (!(await adopt(sessionKey(ctx)))) return;
        const id = `ui-${++state.seq}`;
        state.prompts.push({ id, kind });
        await blocked(id, kind, String(event?.title ?? ""));
      });
    })));

    track(pi.on("ui_prompt_end", guard("ui_prompt_end", (event, ctx) => {
      void run("ui_prompt_end", async () => {
        const kind = String(event?.kind ?? "");
        if (kind !== "confirm" && kind !== "select") return;
        if (!(await adopt(sessionKey(ctx)))) return;
        let index = -1;
        for (let i = state.prompts.length - 1; i >= 0; i--) {
          if (state.prompts[i].kind === kind) {
            index = i;
            break;
          }
        }
        if (index < 0) index = state.prompts.length - 1;
        if (index < 0) return;
        const [prompt] = state.prompts.splice(index, 1);
        await resolve(prompt.id);
      });
    })));

    track(pi.on("session_start", guard("session_start", (_event, ctx) => {
      void run("session_start", async () => {
        // a reload/new/resume/fork gets a fresh state; the old lane, if any,
        // is released by session_shutdown before this, and adopt() is the
        // safety net when it was not.
        await adopt(sessionKey(ctx));
      });
    })));

    track(pi.on("session_shutdown", guard("session_shutdown", () =>
      run("session_shutdown", async () => {
        await endLane();
        dispose();
      }),
    )));
  } catch (err) {
    sayOnce("register", `registration failed: ${scrub(err, 120)}`);
    dispose();
    return undefined;
  }

  function dispose(): void {
    if (disposed) return;
    disposed = true;
    stopHeartbeat();
    state.pending.clear();
    state.prompts.length = 0;
    for (const off of unsubscribers.splice(0)) {
      try {
        off();
      } catch {
        /* the runtime is going away anyway */
      }
    }
    activeDispose = null;
    debug("disposed");
  }

  activeDispose = dispose;
  debug(`loaded for ${state.key || "the current session"} (panel=${url}, token=${token ? "yes" : "no"})`);
  return dispose;
}

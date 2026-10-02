// Live updates: a coalesced snapshot stream over fetch() + ReadableStream.
//
// EventSource cannot send X-LED-Token, and cookies were deliberately avoided,
// so the SSE wire format is parsed by hand. If the stream fails twice the
// client falls back to 2 s polling (paused while the tab is hidden) and tries
// the stream again later.

import { api, getToken } from "./api.js";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function createEventStream({ onSnapshot, onState }) {
  let stopped = false;
  let failures = 0;
  let mode = "connecting";
  let controller = null;
  let pollTimer = null;
  let retryTimer = null;
  let onVisible = null;

  const setMode = (next) => {
    if (next === mode) return;
    mode = next;
    onState?.(next);
  };

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (onVisible) { document.removeEventListener("visibilitychange", onVisible); onVisible = null; }
  }

  async function pollOnce() {
    if (document.hidden || stopped) return;
    try {
      const snapshot = await api("/ui/api/status", { timeout: 4000 });
      onSnapshot?.(snapshot);
      setMode("polling");
    } catch {
      setMode("offline");
    }
  }

  function startPolling() {
    if (pollTimer) return;
    setMode("polling");
    pollOnce();
    pollTimer = setInterval(pollOnce, 2000);
    onVisible = () => { if (!document.hidden) pollOnce(); };
    document.addEventListener("visibilitychange", onVisible);
    retryTimer = setTimeout(() => {
      if (stopped) return;
      stopPolling();
      failures = 0;
      streamLoop();
    }, 30000);
  }

  async function streamLoop() {
    while (!stopped) {
      controller = new AbortController();
      try {
        const response = await fetch("/ui/api/events", {
          headers: getToken() ? { "X-LED-Token": getToken() } : {},
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(`stream HTTP ${response.status}`);
        failures = 0;
        setMode("live");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let cut;
          while ((cut = buffer.indexOf("\n\n")) >= 0) {
            const chunk = buffer.slice(0, cut);
            buffer = buffer.slice(cut + 2);
            const data = [];
            for (const line of chunk.split("\n")) {
              if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
            }
            if (!data.length) continue;
            try { onSnapshot?.(JSON.parse(data.join("\n"))); } catch { /* ignore a bad frame */ }
          }
        }
        throw new Error("stream closed");
      } catch (err) {
        if (stopped) return;
        failures += 1;
        setMode("offline");
        if (failures >= 2) { startPolling(); return; }
        await sleep(Math.min(5000, 500 * 2 ** failures));
      }
    }
  }

  streamLoop();

  return {
    stop() {
      stopped = true;
      controller?.abort();
      stopPolling();
      if (retryTimer) clearTimeout(retryTimer);
    },
  };
}

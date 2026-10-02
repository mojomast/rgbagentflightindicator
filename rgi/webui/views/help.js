// Help: shortcuts, the words this UI uses, and where the integration docs are.

import { h } from "../lib/dom.js";

const SHORTCUTS = [
  ["Ctrl + Z", "Undo the last draft edit"],
  ["Ctrl + Shift + Z", "Redo"],
  ["Ctrl + S", "Apply the draft"],
  ["?", "Open this page"],
  ["Esc", "Cancel a key capture or close a dialog"],
  ["Arrow keys", "Move between keys on a diagram"],
  ["Shift + click", "Extend a diagram selection"],
  ["Enter / Space", "Select the focused key"],
];

const GLOSSARY = [
  ["lane", "A numbered slot that one agent session holds. Lanes are mapped onto lamps."],
  ["lamp", "One addressable light on a device. A keyboard may expose 13 or 126 of them."],
  ["ident", "An agent's own stable name for the machine or project it runs on; overrides the agent name when both match."],
  ["pool", "The ordered lamps that lanes 0..N-1 are painted on."],
  ["state", "What an agent is doing: working, done, blocked, error, idle. Colour is decoration; the icon and label carry the meaning."],
  ["stale", "The reporter has sent no heartbeat for settings.stale_s. The lane keeps its last state and starts ticking from the last change; it may be a dead reporter, so the UI marks it stale instead of pretending nothing happened."],
  ["acked", "A lane you have marked as seen. A done lane becomes idle; a blocked lane keeps its colour but stops blinking and stops notifying until its next state change."],
  ["overlay", "A time-boxed frame for testing or mapping. The render loop consumes it and then restores the lanes."],
];

const DOCS = [
  ["integrations.md", "Every supported agent harness"],
  ["claude-code.md", "Claude Code hooks"],
  ["gemini-cli.md", "Gemini CLI hooks"],
  ["opencode.md", "OpenCode plugin and watcher"],
  ["zoo-code.md", "Zoo Code CLI stream"],
  ["AGENT_PROMPT.md", "Prompt to hand an agent"],
  ["PLUGIN_SETUP_PROMPT.md", "Set up the sidebar plugin"],
  ["integrations.md", "Version matrix"],
];

export function render(ctx) {
  const root = h("div", { class: "stack" });
  root.appendChild(h("h1", {}, "Help"));
  root.appendChild(h("p", { class: "muted" },
    "Everything here is served by your own daemon; nothing leaves this machine."));

  root.appendChild(h("h2", {}, "Shortcuts"));
  root.appendChild(h("table", { class: "table" },
    h("tbody", null, SHORTCUTS.map(([keys, what]) => h("tr", null,
      h("td", null, h("span", { class: "kbd" }, keys)),
      h("td", null, what))))));

  root.appendChild(h("h2", {}, "Words used in this UI"));
  root.appendChild(h("dl", null, GLOSSARY.map(([term, meaning]) => h("div", null,
    h("dt", null, h("strong", {}, term)), h("dd", { class: "muted" }, meaning)))));

  root.appendChild(h("h2", {}, "Integrations"));
  root.appendChild(h("ul", null, DOCS.map(([file, label]) => h("li", null,
    h("a", { href: `/files/${file}`, target: "_blank", rel: "noreferrer" }, label)))));
  root.appendChild(h("p", { class: "muted small" },
    "These links open the raw published files; they are the same text the setup prompts use."));

  root.appendChild(h("h2", {}, "How the panel works"));
  root.appendChild(h("p", { class: "muted" },
    "Agents POST session events to the daemon. The daemon assigns each session a lane, renders the lane states to a colour per lamp, and writes a frame only when something changes. While you are typing it writes nothing at all, so a status light can never cost you a keystroke."));
  root.appendChild(h("p", { class: "muted" },
    "The draft / Apply model: every control in this UI edits a draft in your browser. Hardware is only touched when you press Apply, or a time-boxed test button."));
  root.appendChild(h("h2", {}, "The fleet table"));
  root.appendChild(h("p", { class: "muted" },
    "Context is the model context window in use: it goes amber at 70% and red at 90%. Cost is the spend the session reports. For is the time in the current state and keeps ticking from the daemon's timestamps even when a report is stale. The While you were away card is built from the daemon's history ring; Mark all seen moves it forward, and nothing is shown when history is empty or unavailable."));
  return root;
}

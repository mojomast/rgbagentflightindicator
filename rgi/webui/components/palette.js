// Command palette: Ctrl/⌘+K, fuzzy filter, keyboard-first, ARIA combobox.

import { h, clear } from "../lib/dom.js";

export function openPalette(ctx) {
  const actions = buildActions(ctx);
  const input = h("input", {
    class: "input", type: "search", role: "combobox", autocomplete: "off",
    "aria-expanded": "true", "aria-controls": "palette-list",
    "aria-activedescendant": "", placeholder: "Type a command…",
  });
  const list = h("ul", { id: "palette-list", role: "listbox", class: "palette-list" });
  const dialog = h("dialog", { class: "palette", "aria-label": "Command palette" },
    h("div", { class: "palette-box" }, input, list));
  document.body.appendChild(dialog);
  dialog.showModal();

  let filtered = actions;
  let cursor = 0;

  function render() {
    clear(list);
    filtered.forEach((action, index) => {
      const id = `palette-item-${index}`;
      const item = h("li", {
        id, role: "option", "aria-selected": index === cursor ? "true" : "false",
        class: index === cursor ? "active" : "",
        onclick: () => run(action),
        onmousemove: () => { cursor = index; render(); },
      },
        h("span", {}, action.label),
        action.hint ? h("span", { class: "muted small" }, action.hint) : null);
      list.appendChild(item);
    });
    input.setAttribute("aria-activedescendant",
      filtered.length ? `palette-item-${cursor}` : "");
  }

  function filter(query) {
    const needle = query.trim().toLowerCase();
    filtered = !needle ? actions : actions.map((action) => {
      const haystack = `${action.label} ${action.hint || ""} ${action.group || ""}`.toLowerCase();
      const index = haystack.indexOf(needle);
      return { action, score: index < 0 ? Infinity : index };
    }).filter((entry) => Number.isFinite(entry.score))
      .sort((a, b) => a.score - b.score)
      .map((entry) => entry.action);
    cursor = 0;
    render();
  }

  function close() {
    dialog.close();
    dialog.remove();
  }

  function run(action) {
    close();
    try { action.run(); } catch (err) { ctx.toast(String(err.message || err), "bad"); }
  }

  input.addEventListener("input", () => filter(input.value));
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      cursor = Math.min(filtered.length - 1, cursor + 1);
      render();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      cursor = Math.max(0, cursor - 1);
      render();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (filtered[cursor]) run(filtered[cursor]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  });
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
  filter("");
  input.focus();
}

function buildActions(ctx) {
  const modes = [
    ["Live", ""], ["Layout", "layout"], ["Paint", "paint"],
    ["Identify", "identify"], ["Mapping", "mapping"],
    ["States", "appearance"], ["Devices", "devices"], ["Agents", "agents"],
    ["Settings", "settings"], ["Logs", "logs"], ["Help", "help"],
  ];
  const actions = modes.map(([label, route]) => ({
    group: "Go to", label: `Go to ${label}`,
    run: () => ctx.go(route ? `#/${route}` : "#/"),
  }));
  actions.push(
    { group: "Draft", label: "Apply to board", hint: "Ctrl+S", run: () => ctx.apply() },
    { group: "Draft", label: "Discard changes", run: () => ctx.discard() },
    { group: "Draft", label: "Undo", hint: "Ctrl+Z", run: () => ctx.undo() },
    { group: "Draft", label: "Redo", hint: "Ctrl+Shift+Z", run: () => ctx.redo() },
    { group: "Board", label: "Flash selected lamps (5 s)",
      run: () => ctx.hardwareTest({
        device: ctx.deviceName,
        lamps: ctx.selection.map((lamp) => ({ index: lamp, rgb: [255, 255, 255] })),
        duration_ms: 5000,
      }) },
    { group: "Board", label: "Stop hardware test", run: () => ctx.stopTest() },
    { group: "Board", label: "Clear all lanes", run: async () => {
        if (await ctx.confirm("Clear every lane?",
            "Sessions will re-report on their next update.")) {
          await ctx.api("/clear", { method: "POST", body: {} });
          ctx.refreshStatus();
        }
      } },
    { group: "Help", label: "Keyboard shortcuts", run: () => ctx.go("#/help") },
  );
  if (ctx.selection.length) {
    actions.push({
      group: "Selection", label: `Lane-assign ${ctx.selection.length} lamp(s)`,
      run: () => ctx.go("#/paint"),
    });
  }
  return actions;
}

// Reusable UI pieces: form rows, chips, banners, toasts, dialogs.

import { h, clear } from "../lib/dom.js";

export function button(label, onClick, opts = {}) {
  const { variant = "", title = "", disabled = false, type = "button", sm = false } = opts;
  return h("button", {
    class: `btn${sm ? " sm" : ""} ${variant}`.trim(),
    type,
    title,
    disabled,
    onclick: onClick,
  }, label);
}

export function field(labelText, control, { hint = "", error = "", id } = {}) {
  const label = h("label", { for: id || undefined }, labelText);
  const wrap = h("div", { class: "field" }, label, control);
  if (hint) wrap.appendChild(h("span", { class: "hint" }, hint));
  if (error) wrap.appendChild(h("span", { class: "error", role: "alert" }, error));
  return wrap;
}

export function textInput(value, onChange, opts = {}) {
  const { placeholder = "", mono = false, type = "text", ariaLabel = "", onInput } = opts;
  return h("input", {
    class: `input${mono ? " mono" : ""}`,
    type,
    value: value ?? "",
    placeholder,
    "aria-label": ariaLabel || undefined,
    oninput: onInput,
    onchange: (event) => onChange(event.target.value),
  });
}

export function numberInput(value, onChange, opts = {}) {
  const { min = 0, max = 1000000, step = 1, ariaLabel = "", onInput } = opts;
  return h("input", {
    class: "input num",
    type: "number",
    value: value ?? 0,
    min, max, step,
    "aria-label": ariaLabel || undefined,
    oninput: onInput,
    onchange: (event) => onChange(Math.max(min, Math.min(max, Number(event.target.value)))),
  });
}

export function selectInput(options, value, onChange, opts = {}) {
  const el = h("select", { class: "select", "aria-label": opts.ariaLabel || undefined,
                           onchange: (event) => onChange(event.target.value) });
  for (const option of options) {
    const { value: optionValue, label } = typeof option === "string"
      ? { value: option, label: option } : option;
    el.appendChild(h("option", { value: optionValue, selected: optionValue === value }, label));
  }
  return el;
}

export function checkbox(labelText, checked, onChange) {
  return h("label", { class: "check" },
    h("input", { type: "checkbox", checked, onchange: (e) => onChange(e.target.checked) }),
    labelText);
}

export function colorField(labelText, value, onChange, { hint = "" } = {}) {
  const hex = textInput(value, (next) => {
    if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(next.trim())) onChange(next.trim());
  }, { mono: true, ariaLabel: `${labelText} hex`, onInput: (e) => {
    const next = e.target.value.trim();
    if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(next)) onChange(next);
  }});
  const picker = h("input", {
    type: "color",
    value: /^#[0-9a-f]{6}$/i.test(value) ? value : "#000000",
    "aria-label": `${labelText} picker`,
    oninput: (event) => onChange(event.target.value),
  });
  return field(labelText, h("div", { class: "row" }, picker, hex), { hint });
}

export function chip(state, label) {
  return h("span", { class: "chip", "data-state": state || "off" },
    h("span", { class: "dot", "aria-hidden": "true" }), label || state || "off");
}

export function banner(kind, text, actions = []) {
  return h("div", { class: `banner ${kind}` },
    h("div", {}, text),
    actions.length ? h("div", { class: "row" }, actions) : null);
}

export function toast(text, kind = "", ms = 4200) {
  const host = document.getElementById("toaster");
  if (!host) return;
  const el = h("div", { class: `toast ${kind}`.trim(), role: "status" }, text);
  host.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

export function dialog({ title, body, actions = [{ label: "Close", value: null, primary: true }] }) {
  return new Promise((resolve) => {
    const node = h("dialog", { "aria-label": title });
    const close = (value) => {
      node.close();
      node.remove();
      resolve(value);
    };
    node.appendChild(h("h2", {}, title));
    if (body) node.appendChild(body);
    const row = h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "12px" } });
    for (const action of actions) {
      row.appendChild(button(action.label, () => close(action.value),
        { variant: action.primary ? "primary" : "", disabled: action.disabled }));
    }
    node.appendChild(row);
    node.addEventListener("cancel", (event) => { event.preventDefault(); close(null); });
    node.addEventListener("close", () => { if (node.isConnected) { node.remove(); resolve(null); } });
    document.body.appendChild(node);
    node.showModal();
    const focusable = node.querySelector("input, select, textarea, button");
    focusable?.focus();
  });
}

export async function confirmDialog(title, message, confirmLabel = "Confirm") {
  const result = await dialog({
    title,
    body: h("p", {}, message),
    actions: [
      { label: "Cancel", value: false },
      { label: confirmLabel, value: true, primary: true },
    ],
  });
  return result === true;
}

export async function promptDialog(title, { label = "Value", value = "", hint = "", type = "text" } = {}) {
  const input = textInput(value, () => {}, { type });
  const result = await dialog({
    title,
    body: field(label, input, { hint }),
    actions: [
      { label: "Cancel", value: null },
      { label: "Save", value: "save", primary: true },
    ],
  });
  return result === "save" ? input.value : null;
}

export function table(headers, rows, caption = "") {
  const thead = h("thead", {}, h("tr", {}, headers.map((head) =>
    h("th", { scope: "col" }, head))));
  const tbody = h("tbody", {}, rows);
  return h("table", { class: "table" },
    caption ? h("caption", {}, caption) : null, thead, tbody);
}

/** One-shot physical key capture: press a key, read event.code. Esc cancels. */
export function captureKey(onCode, { seconds = 8 } = {}) {
  toast("Listening for a key press… (Esc to cancel)");
  const done = () => {
    clearTimeout(timer);
    document.removeEventListener("keydown", handler, true);
  };
  const handler = (event) => {
    if (event.key === "Escape") { done(); return; }
    if (event.repeat) return;
    event.preventDefault();
    event.stopPropagation();
    done();
    onCode(event.code);
  };
  const timer = setTimeout(done, seconds * 1000);
  document.addEventListener("keydown", handler, true);
}

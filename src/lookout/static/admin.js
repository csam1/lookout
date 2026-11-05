"use strict";

// All server data is rendered as text nodes (never innerHTML).

const KEY_STORE = "lookout.adminKey";
const SVG_NS = "http://www.w3.org/2000/svg";

const $ = (id) => document.getElementById(id);
const keyInput = $("admin-key");
const keyPill = $("key-pill");
const keyEntry = $("key-entry");
const errorBox = $("error");
const gate = $("gate");
const content = $("content");
const createForm = $("create-form");

const number = new Intl.NumberFormat();
const state = { keys: [], daily: [], days: 30, filter: "", editingId: null, confirmingId: null };

// ---- helpers ----------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else node.setAttribute(name, value);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  return node;
}

function loadKey() {
  try { return sessionStorage.getItem(KEY_STORE) || ""; } catch { return ""; }
}

function saveKey(key) {
  try {
    if (key) sessionStorage.setItem(KEY_STORE, key);
    else sessionStorage.removeItem(KEY_STORE);
  } catch { /* storage unavailable */ }
}

function currentKey() {
  return keyInput.value.trim();
}

function shortDate(value) {
  const d = typeof value === "string" && value.length === 10 ? new Date(`${value}T00:00:00Z`) : new Date(value);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", timeZone: "UTC" });
}

function fullDate(value) {
  return new Date(value).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function monthName(value) {
  return new Date(value).toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
}

function ago(value) {
  if (!value) return "Never";
  const seconds = Math.max(0, (Date.now() - Date.parse(value)) / 1000);
  if (seconds < 60) return "Just now";
  const units = [["day", 86400], ["hour", 3600], ["minute", 60]];
  for (const [unit, size] of units) {
    if (seconds >= size) {
      const n = Math.floor(seconds / size);
      return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
    }
  }
  return "Just now";
}

class ApiFailure extends Error {
  constructor(status, body) {
    super(`HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { Authorization: `Bearer ${currentKey()}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) throw new ApiFailure(resp.status, data);
  return data;
}

// ---- errors -------------------------------------------------------------------

function showError(title, detail, requestId) {
  errorBox.replaceChildren(
    el("p", {}, el("strong", {}, title)),
    detail ? el("p", { class: "error-detail" }, detail) : null,
    requestId ? el("p", { class: "error-detail" }, `Request ID ${requestId}`) : null
  );
  errorBox.hidden = false;
}

function hideError() {
  errorBox.hidden = true;
  errorBox.replaceChildren();
}

function explain(error) {
  if (!(error instanceof ApiFailure)) {
    return showError("Could not reach the API.", "Check that the server is running at this address.");
  }
  const err = (error.body && error.body.error) || {};
  const id = err.request_id;
  switch (error.status) {
    case 401:
      return showError("That key was not accepted.", "It may be wrong or revoked.", id);
    case 403:
      return showError("That key works, but it isn't an admin key.",
        "Create an admin key on the server: lookout create-key --name your-name --admin", id);
    case 409:
      return showError("That change was not made.", err.message, id);
    case 422:
      return showError("Check the values entered.", err.message, id);
    default:
      return showError(`The request failed (HTTP ${error.status}).`, err.message, id);
  }
}

// ---- key pill ---------------------------------------------------------------------

function showSignedIn() {
  $("key-prefix").textContent = `${currentKey().slice(0, 7)}…`;
  keyPill.hidden = false;
  keyEntry.hidden = true;
}

function showSignedOut() {
  keyPill.hidden = true;
  keyEntry.hidden = false;
}

// ---- month tiles -------------------------------------------------------------

function renderFacts(usage) {
  $("period").textContent = `${monthName(usage.period_start)}, from the 1st to today, UTC`;
  const cacheShare = usage.calls ? `${Math.round((usage.cache_hits / usage.calls) * 100)}` : "0";
  const tile = (label, value, unit, note) =>
    el("div", {},
      el("dt", {}, label),
      el("dd", {}, value, unit ? el("small", {}, unit) : null),
      el("span", { class: "tile-note" }, note));
  const revoked = state.keys.filter((k) => !k.active).length;
  $("facts").replaceChildren(
    tile("Credits used", number.format(usage.credits), null, `${number.format(usage.calls)} searches this month`),
    tile("Searches", number.format(usage.calls), null,
      `${number.format(usage.fast_calls)} fast · ${number.format(usage.deep_calls)} deep`),
    tile("Served from cache", cacheShare, "%", `${number.format(usage.cache_hits)} responses`),
    tile("Keys used", number.format(usage.keys_used), ` / ${number.format(usage.active_keys)}`,
      revoked ? `${number.format(usage.active_keys)} active, ${number.format(revoked)} revoked` : `${number.format(usage.active_keys)} active`)
  );
}

// ---- chart -------------------------------------------------------------------

function niceMax(value) {
  if (value <= 0) return 10;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (step * power >= value) return step * power;
  }
  return 10 * power;
}

function barPath(x, base, width, height, radius) {
  if (height <= 0) return "";
  const r = Math.min(radius, width / 2, height);
  const top = base - height;
  return `M${x},${base}V${top + r}Q${x},${top} ${x + r},${top}H${x + width - r}Q${x + width},${top} ${x + width},${top + r}V${base}Z`;
}

function renderChart() {
  const frame = $("chart");
  const tooltip = $("tooltip");
  const daily = state.daily;
  const width = Math.max(280, frame.clientWidth);
  const height = 200;
  const margin = { top: 8, right: 4, bottom: 24, left: 40 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const base = margin.top + plotH;
  const top = niceMax(Math.max(0, ...daily.map((d) => d.credits)));
  const band = plotW / daily.length;
  const gap = daily.length > 40 ? 1 : 3;

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, "aria-hidden": "true" });

  for (const tick of [0, top / 4, top / 2, (3 * top) / 4, top]) {
    const y = base - (tick / top) * plotH;
    root.append(svg("line", { class: tick === 0 ? "baseline" : "gridline", x1: margin.left, x2: width - margin.right, y1: y, y2: y }));
    if (tick === 0 || tick === top / 2 || tick === top) {
      const label = svg("text", { class: "tick", x: margin.left - 8, y: y + 4, "text-anchor": "end" });
      label.textContent = number.format(tick);
      root.append(label);
    }
  }

  const labelAt = new Set([0, Math.floor((daily.length - 1) / 2), daily.length - 1]);
  daily.forEach((day, i) => {
    const x = margin.left + i * band;
    const w = Math.max(1, band - gap);
    const fastCredits = day.credits - day.deep_credits;
    const hFast = (fastCredits / top) * plotH;
    const hDeep = (day.deep_credits / top) * plotH;
    const group = svg("g");
    // Fast at the bottom, deep stacked on top; the top segment gets the rounded corners.
    if (hFast > 0) {
      group.append(svg("path", { class: "bar-fast", d: hDeep > 0 ? `M${x + gap / 2},${base}V${base - hFast}H${x + gap / 2 + w}V${base}Z` : barPath(x + gap / 2, base, w, hFast, 3) }));
    }
    if (hDeep > 0) group.append(svg("path", { class: "bar-deep", d: barPath(x + gap / 2, base - hFast, w, hDeep, 3) }));
    root.append(group);

    if (labelAt.has(i)) {
      const anchor = i === 0 ? "start" : i === daily.length - 1 ? "end" : "middle";
      const tx = i === 0 ? x : i === daily.length - 1 ? x + band : x + band / 2;
      const label = svg("text", { class: "tick", x: tx, y: height - 6, "text-anchor": anchor });
      label.textContent = shortDate(day.date);
      root.append(label);
    }

    // Hit target: the whole column, much wider and taller than the bar itself.
    const hit = svg("rect", { class: "hit", x, y: margin.top, width: band, height: plotH });
    const show = () => {
      group.classList.add("is-hover");
      tooltip.replaceChildren(
        el("strong", {}, shortDate(day.date)),
        el("span", { class: "mono" }, `${number.format(day.credits)} credits`),
        el("br"),
        el("span", { class: "mono" }, `${number.format(day.deep_credits)} deep · ${number.format(fastCredits)} fast`),
        el("br"),
        el("span", { class: "mono" }, `${number.format(day.calls)} searches`)
      );
      tooltip.hidden = false;
      const figure = frame.parentElement.getBoundingClientRect();
      const frameBox = frame.getBoundingClientRect();
      const centre = frameBox.left - figure.left + x + band / 2;
      const tipWidth = tooltip.offsetWidth;
      const left = Math.min(Math.max(centre - tipWidth / 2, 4), figure.width - tipWidth - 4);
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${frameBox.top - figure.top + base - hFast - hDeep - tooltip.offsetHeight - 8}px`;
    };
    const hide = () => {
      group.classList.remove("is-hover");
      tooltip.hidden = true;
    };
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("pointerdown", show);
    hit.addEventListener("pointerleave", hide);
    root.append(hit);
  });

  frame.replaceChildren(root);

  const total = daily.reduce((sum, d) => sum + d.credits, 0);
  const busiest = daily.reduce((best, d) => (d.credits > best.credits ? d : best), daily[0]);
  $("chart-summary").textContent = total
    ? `${number.format(total)} credits over ${daily.length} days. Busiest day ${shortDate(busiest.date)} with ${number.format(busiest.credits)}.`
    : `No credits used in the last ${daily.length} days.`;
}

function renderDailyTable() {
  $("daily-rows").replaceChildren(
    ...[...state.daily].reverse().map((d) =>
      el("tr", {}, el("td", {}, shortDate(d.date)), el("td", { class: "num" }, number.format(d.calls)),
        el("td", { class: "num" }, number.format(d.deep_credits)), el("td", { class: "num" }, number.format(d.credits))))
  );
}

// ---- keys table -----------------------------------------------------------

function meter(key) {
  const share = key.monthly_credit_limit ? key.credits_this_month / key.monthly_credit_limit : 0;
  const fill = el("span");
  fill.style.width = `${Math.min(100, share * 100).toFixed(1)}%`;
  return el("div", { class: share >= 0.8 ? "meter high" : "meter" },
    el("div", { class: "meter-text" },
      number.format(key.credits_this_month), el("span", {}, ` / ${number.format(key.monthly_credit_limit)}`)),
    el("div", { class: "meter-bar", "aria-hidden": "true" }, fill)
  );
}

function actionsFor(key) {
  if (!key.active) return null;
  if (state.confirmingId === key.id) {
    const confirmButton = el("button", { type: "button", class: "button-danger" }, "Revoke key");
    const cancel = el("button", { type: "button", class: "button" }, "Cancel");
    confirmButton.addEventListener("click", () => revokeKey(key));
    cancel.addEventListener("click", () => { state.confirmingId = null; renderKeys(); });
    return el("span", { class: "confirm" },
      "Anything using this key stops working now. This can't be undone.",
      confirmButton, cancel);
  }
  const edit = el("button", { type: "button", class: "button" }, "Edit");
  const revoke = el("button", { type: "button", class: "button" }, "Revoke");
  edit.addEventListener("click", () => { state.editingId = key.id; state.confirmingId = null; renderKeys(); });
  revoke.addEventListener("click", () => { state.confirmingId = key.id; state.editingId = null; renderKeys(); });
  return el("span", { class: "row-actions" }, edit, revoke);
}

function editRow(key) {
  const name = el("input", { type: "text", maxlength: "100", value: key.name, "aria-label": "Name" });
  const limit = el("input", { type: "number", min: "1", step: "1", value: String(key.monthly_credit_limit), "aria-label": "Monthly credit limit" });
  const save = el("button", { type: "button", class: "button-primary" }, "Save");
  const cancel = el("button", { type: "button", class: "button" }, "Cancel");
  save.addEventListener("click", () => updateKey(key, name.value.trim(), parseInt(limit.value, 10)));
  cancel.addEventListener("click", () => { state.editingId = null; renderKeys(); });
  for (const input of [name, limit]) {
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") save.click();
      if (event.key === "Escape") cancel.click();
    });
  }
  setTimeout(() => name.focus(), 0);
  return el("tr", { class: "edit-row" },
    el("td", { "data-label": "Name" }, name),
    el("td", { class: "prefix", "data-label": "Key" }, `${key.key_prefix}…`),
    el("td", { "data-label": "Monthly limit" }, limit),
    el("td", { class: "num", "data-label": "Calls" }, number.format(key.calls_this_month)),
    el("td", { class: "when", "data-label": "Last used" }, ago(key.last_used_at)),
    el("td", { class: "when", "data-label": "Created" }, fullDate(key.created_at)),
    el("td", {}, el("span", { class: "row-actions" }, save, cancel))
  );
}

function visibleKeys() {
  const needle = state.filter.trim().toLowerCase();
  if (!needle) return state.keys;
  return state.keys.filter((k) => k.name.toLowerCase().includes(needle) || k.key_prefix.toLowerCase().includes(needle));
}

function renderKeys() {
  const keys = visibleKeys();
  const rows = keys.map((key) => {
    if (key.active && state.editingId === key.id) return editRow(key);
    return el("tr", { class: key.active ? "" : "revoked" },
      el("td", {},
        el("span", { class: "key-name" }, key.name),
        key.is_admin ? el("span", { class: "role" }, "Admin") : null,
        key.active ? null : el("span", { class: "role revoked" }, "Revoked")),
      el("td", { class: "prefix", "data-label": "Key" }, `${key.key_prefix}…`),
      el("td", { "data-label": "Credits this month" }, meter(key)),
      el("td", { class: "num", "data-label": "Calls" }, number.format(key.calls_this_month)),
      el("td", { class: "when", "data-label": "Last used" }, ago(key.last_used_at)),
      el("td", { class: "when", "data-label": "Created" }, fullDate(key.created_at)),
      el("td", {}, actionsFor(key))
    );
  });
  const emptyText = state.keys.length ? "No keys match that filter." : "No keys yet. Create one to get started.";
  $("key-rows").replaceChildren(
    ...(rows.length ? rows : [el("tr", { class: "empty-row" }, el("td", { colspan: "7" }, emptyText))])
  );
}

// ---- actions ------------------------------------------------------------------

async function loadUsage() {
  const usage = await api(`/v1/admin/usage?days=${state.days}`);
  state.daily = usage.daily;
  renderFacts(usage);
  renderChart();
  renderDailyTable();
}

async function load() {
  hideError();
  if (!currentKey()) {
    showError("Enter an admin key.");
    keyInput.focus();
    return;
  }
  const button = $("load");
  button.disabled = true;
  try {
    const [usage, keys] = await Promise.all([api(`/v1/admin/usage?days=${state.days}`), api("/v1/admin/keys")]);
    saveKey(currentKey());
    state.keys = keys;
    state.daily = usage.daily;
    gate.hidden = true;
    content.hidden = false;
    showSignedIn();
    renderFacts(usage);
    renderChart();
    renderDailyTable();
    renderKeys();
  } catch (error) {
    content.hidden = true;
    gate.hidden = false;
    showSignedOut();
    explain(error);
  } finally {
    button.disabled = false;
  }
}

function signOut() {
  saveKey("");
  keyInput.value = "";
  state.keys = [];
  state.daily = [];
  content.hidden = true;
  gate.hidden = false;
  hideError();
  showSignedOut();
  keyInput.focus();
}

async function refreshKeys() {
  state.keys = await api("/v1/admin/keys");
  renderKeys();
}

async function updateKey(key, name, limit) {
  hideError();
  if (!name) return showError("Enter a name for the key.");
  if (!Number.isInteger(limit) || limit < 1) return showError("Enter a monthly credit limit of 1 or more.");
  try {
    await api(`/v1/admin/keys/${key.id}`, { method: "PATCH", body: { name, monthly_credit_limit: limit } });
    state.editingId = null;
    await refreshKeys();
  } catch (error) {
    explain(error);
  }
}

async function revokeKey(key) {
  hideError();
  try {
    await api(`/v1/admin/keys/${key.id}/revoke`, { method: "POST" });
    state.confirmingId = null;
    await refreshKeys();
    await loadUsage();
  } catch (error) {
    state.confirmingId = null;
    renderKeys();
    explain(error);
  }
}

async function createKey(event) {
  event.preventDefault();
  hideError();
  const name = $("new-name").value.trim();
  const limit = parseInt($("new-limit").value, 10);
  if (!name) {
    showError("Enter a name for the key.");
    $("new-name").focus();
    return;
  }
  if (!Number.isInteger(limit) || limit < 1) {
    showError("Enter a monthly credit limit of 1 or more.");
    $("new-limit").focus();
    return;
  }
  const submit = createForm.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const created = await api("/v1/admin/keys", {
      method: "POST",
      body: { name, monthly_credit_limit: limit, is_admin: $("new-admin").checked },
    });
    createForm.reset();
    createForm.hidden = true;
    $("show-create").hidden = false;
    $("created-key").textContent = created.api_key;
    $("created").hidden = false;
    $("copy-created").focus();
    await refreshKeys();
    await loadUsage();
  } catch (error) {
    explain(error);
  } finally {
    submit.disabled = false;
  }
}

async function copyCreated() {
  const button = $("copy-created");
  try {
    await navigator.clipboard.writeText($("created-key").textContent);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Copy failed, select the key instead";
  }
  setTimeout(() => { button.textContent = "Copy key"; }, 1800);
}

// ---- wiring ---------------------------------------------------------------------

$("key-form").addEventListener("submit", (event) => {
  event.preventDefault();
  load();
});

$("sign-out").addEventListener("click", signOut);

for (const radio of document.querySelectorAll('input[name="days"]')) {
  radio.addEventListener("change", async () => {
    state.days = parseInt(radio.value, 10);
    try {
      await loadUsage();
    } catch (error) {
      explain(error);
    }
  });
}

$("toggle-table").addEventListener("click", (event) => {
  const button = event.currentTarget;
  const open = button.getAttribute("aria-expanded") !== "true";
  button.setAttribute("aria-expanded", String(open));
  $("daily-table").hidden = !open;
});

$("key-filter").addEventListener("input", (event) => {
  state.filter = event.target.value;
  renderKeys();
});

$("show-create").addEventListener("click", () => {
  createForm.hidden = false;
  $("show-create").hidden = true;
  $("new-name").focus();
});

$("cancel-create").addEventListener("click", () => {
  createForm.reset();
  createForm.hidden = true;
  $("show-create").hidden = false;
});

createForm.addEventListener("submit", createKey);
$("copy-created").addEventListener("click", copyCreated);
$("dismiss-created").addEventListener("click", () => {
  $("created-key").textContent = ""; // remove the plaintext key from the page
  $("created").hidden = true;
});

let resizeFrame = null;
new ResizeObserver(() => {
  if (!state.daily.length || content.hidden) return;
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(renderChart);
}).observe($("chart"));

keyInput.value = loadKey();
if (keyInput.value) load();

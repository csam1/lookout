"use strict";

// Search results are untrusted web text. Everything below renders them as text nodes
// (never innerHTML) and only links to http(s) URLs.

// Must match excerpt.py so highlights show the words that drove passage selection.
const STOPWORDS = new Set(
  ("a an and are as at be by for from has have how in is it its of on or that the this " +
    "these those to was were what when which who why will with about into than then there").split(" ")
);
const KEY_STORE = "lookout.apiKey";
const TOKEN = /[\p{L}\p{N}_]+/gu;

const $ = (id) => document.getElementById(id);
const form = $("search-form");
const keyForm = $("key-form");
const keyInput = $("api-key");
const keyPill = $("key-pill");
const keyEntry = $("key-entry");
const queryInput = $("query");
const objectiveInput = $("objective");
const submitButton = $("submit");
const submitText = $("submit-text");
const rawContent = $("raw-content");
const emptyState = $("empty");
const errorBox = $("error");
const summary = $("summary");
const resultsHead = $("results-head");
const resultList = $("result-list");
const requestBox = $("request");

let inFlight = null;
let lastRequestBody = null;
let lastResponse = null;

// ---- small helpers -------------------------------------------------------

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

function safeUrl(raw) {
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

const number = new Intl.NumberFormat();

function formatDate(raw) {
  const parsed = Date.parse(raw);
  if (Number.isNaN(parsed)) return raw;
  return new Date(parsed).toLocaleDateString(undefined, { month: "short", year: "numeric" });
}

function seconds(ms) {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function loadKey() {
  try { return sessionStorage.getItem(KEY_STORE) || ""; } catch { return ""; }
}

function saveKey(key) {
  try {
    if (key) sessionStorage.setItem(KEY_STORE, key);
    else sessionStorage.removeItem(KEY_STORE);
  } catch { /* storage unavailable: key just isn't remembered */ }
}

function currentKey() {
  return keyInput.value.trim();
}

function authHeaders() {
  return { Authorization: `Bearer ${currentKey()}` };
}

// ---- key pill in the top bar ---------------------------------------------------

function showKeyPill() {
  const key = currentKey();
  if (!key) return showKeyEntry();
  $("key-prefix").textContent = `${key.slice(0, 7)}…`;
  keyPill.hidden = false;
  keyEntry.hidden = true;
}

function showKeyEntry() {
  keyPill.hidden = true;
  keyEntry.hidden = false;
}

// ---- domain chips ----------------------------------------------------------------

function chipField(containerId) {
  const container = $(containerId);
  const input = container.querySelector("input");
  const values = [];

  function render() {
    for (const chip of container.querySelectorAll(".chip")) chip.remove();
    values.forEach((value, index) => {
      const remove = el("button", { type: "button", "aria-label": `Remove ${value}` }, "×");
      remove.addEventListener("click", () => { values.splice(index, 1); render(); input.focus(); });
      container.insertBefore(el("span", { class: "chip" }, value, remove), input);
    });
  }

  function commit() {
    const parts = input.value.split(/[\s,]+/).map((d) => d.trim().toLowerCase()).filter(Boolean);
    for (const part of parts) if (!values.includes(part)) values.push(part);
    input.value = "";
    render();
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === "," || event.key === " ") {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) return; // search shortcut
      event.preventDefault();
      commit();
    } else if (event.key === "Backspace" && !input.value && values.length) {
      values.pop();
      render();
    }
  });
  input.addEventListener("blur", commit);
  container.addEventListener("click", (event) => {
    if (event.target === container) input.focus();
  });

  return { values: () => { commit(); return [...values]; } };
}

const includeDomains = chipField("include-chips");
const excludeDomains = chipField("exclude-chips");

// ---- highlighting ------------------------------------------------------------

function termsFrom(text) {
  const words = (text.toLowerCase().match(TOKEN) || []).filter((t) => t.length > 1 && !STOPWORDS.has(t));
  return [...new Set(words)].sort((a, b) => b.length - a.length);
}

function highlighted(text, terms) {
  const fragment = document.createDocumentFragment();
  if (!terms.length) {
    fragment.append(text);
    return fragment;
  }
  const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(`(?<![\\p{L}\\p{N}_])(?:${escaped.join("|")})(?![\\p{L}\\p{N}_])`, "giu");

  // Merge matches separated only by spaces, so a matched phrase reads as one highlight.
  const ranges = [];
  for (const match of text.matchAll(pattern)) {
    const start = match.index;
    const end = start + match[0].length;
    const previous = ranges[ranges.length - 1];
    if (previous && /^[ \t]+$/.test(text.slice(previous[1], start))) previous[1] = end;
    else ranges.push([start, end]);
  }

  let last = 0;
  for (const [start, end] of ranges) {
    if (start > last) fragment.append(text.slice(last, start));
    fragment.append(el("mark", {}, text.slice(start, end)));
    last = end;
  }
  if (last < text.length) fragment.append(text.slice(last));
  return fragment;
}

// ---- fetch status in plain words ------------------------------------------

function describeStatus(status) {
  if (status === null || status === undefined) return { state: "plain", label: "Snippet" };
  if (status === "ok") return { state: "read", label: "Read" };
  const fallback = (reason) => ({ state: "fallback", label: `${reason}, snippet shown`, code: status });
  if (status === "timeout") return fallback("Timed out");
  if (status === "robots_disallowed") return fallback("Crawling not allowed");
  if (status.startsWith("blocked:")) return fallback("Blocked by fetch safety rules");
  if (status.startsWith("http_")) return fallback(`Site returned ${status.slice(5)}`);
  if (status === "empty_content") return fallback("No readable text");
  if (status === "unsupported_type") return fallback("Not a web page");
  if (status === "too_large") return fallback("Page too large");
  return fallback("Could not read the page");
}

function statusNote(status) {
  if (status === "timeout") return "Still loading in the background; the next query that returns this page gets the full text.";
  return null;
}

// ---- form ----------------------------------------------------------------

const MODE_NOTES = {
  fast: "Search snippets only. About 1 second, 1 credit.",
  deep: "Reads each page and picks the best passages. Up to 3 seconds, 2 credits.",
};

function selectedMode() {
  return form.querySelector('input[name="mode"]:checked').value;
}

function intField(id, fallback) {
  const value = parseInt($(id).value, 10);
  return Number.isNaN(value) ? fallback : value;
}

function readForm() {
  const body = {
    query: queryInput.value.trim(),
    mode: selectedMode(),
    max_results: intField("max-results", 5),
    max_chars_per_result: intField("max-chars", 1000),
    time_range: $("time-range").value,
  };
  const objective = objectiveInput.value.trim();
  if (objective) body.objective = objective;
  const include = includeDomains.values();
  const exclude = excludeDomains.values();
  if (include.length) body.include_domains = include;
  if (exclude.length) body.exclude_domains = exclude;
  if (body.mode === "deep" && rawContent.checked) body.include_raw_content = true;
  return body;
}

function syncModeOptions() {
  const mode = selectedMode();
  rawContent.disabled = mode !== "deep";
  $("mode-note").textContent = MODE_NOTES[mode];
}

// ---- rendering -------------------------------------------------------------

function clearResults() {
  summary.hidden = true;
  summary.replaceChildren();
  resultsHead.hidden = true;
  resultList.replaceChildren();
}

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

function explainFailure(status, data) {
  const err = data && data.error ? data.error : {};
  const message = err.message || "";
  const id = err.request_id;
  if (status === 401) {
    $("usage").hidden = true;
    showKeyEntry();
    keyInput.focus();
    return showError("That API key was not accepted.", "Check it, or create one with lookout create-key.", id);
  }
  if (status === 422) return showError("Check the search options.", message, id);
  if (status === 429) return showError("This key has used its credits for the month.", `${message} Ask an admin to raise the limit.`, id);
  if (status === 502) {
    return showError("The search provider did not answer.", `${message} Check that SearXNG is running: docker compose up -d searxng`, id);
  }
  return showError(`The search failed (HTTP ${status}).`, message, id);
}

function stat(label, value, unit) {
  return el("div", { class: "stat" },
    el("span", { class: "stat-label" }, label),
    el("span", { class: "stat-value" }, value, unit ? el("small", {}, unit) : null)
  );
}

function renderSummary(data, body, elapsedMs) {
  const results = data.results;
  const latency = elapsedMs < 1000
    ? stat("Latency", String(Math.round(elapsedMs)), "ms")
    : stat("Latency", (elapsedMs / 1000).toFixed(2), "s");
  const read = results.filter((r) => r.fetch_status === "ok").length;
  const pages = body.mode === "deep"
    ? stat("Pages read", String(read), `/ ${results.length}`)
    : stat("Mode", "Fast");
  const cache = el("div", { class: "stat" },
    el("span", { class: "stat-label" }, "Cache"),
    el("span", { class: data.cached ? "stat-flag hit" : "stat-flag" }, data.cached ? "HIT" : "MISS")
  );
  summary.replaceChildren(
    stat("Results", String(results.length)),
    latency,
    stat("Credits", String(data.usage.credits)),
    pages,
    cache
  );
  summary.hidden = false;

  $("results-query").textContent = body.query;
  $("request-id").textContent = data.request_id || "";
  resultsHead.hidden = false;
}

function renderResult(result, index, terms, maxChars) {
  const status = describeStatus(result.fetch_status);
  const href = safeUrl(result.url);
  const titleText = result.title || result.url;
  const title = href
    ? el("a", { class: "result-title", href, target: "_blank", rel: "noopener noreferrer" }, titleText)
    : el("span", { class: "result-title" }, titleText);

  const domain = result.domain || "";
  const head = el("div", { class: "result-head" },
    el("span", { class: "rank" }, String(index + 1).padStart(2, "0")),
    el("span", { class: "favicon", "aria-hidden": "true" }, domain.replace(/^www\./, "").slice(0, 1)),
    el("span", { class: "domain" }, domain),
    result.published_at ? el("span", { class: "date" }, formatDate(result.published_at)) : null,
    el("span", { class: `badge ${status.state}` }, status.label)
  );

  const excerpts = result.excerpts.length
    ? result.excerpts.map((text) => {
        const paragraph = el("p", { class: "excerpt" });
        paragraph.append(highlighted(text, terms));
        return paragraph;
      })
    : [el("p", { class: "no-results" }, "No text came back for this result.")];

  const used = result.excerpts.reduce((total, text) => total + text.length, 0);
  const fill = el("span");
  fill.style.width = `${Math.min(100, (used / maxChars) * 100).toFixed(1)}%`;
  const note = statusNote(result.fetch_status);
  const budget = el("p", { class: "budget" },
    status.code
      ? el("span", { class: "mono" }, `fetch_status: ${status.code}`)
      : el("span", { class: "mono" }, `${number.format(used)} / ${number.format(maxChars)} chars`),
    status.code ? null : el("span", { class: "budget-bar", "aria-hidden": "true" }, fill),
    note ? el("span", {}, `· ${note}`) : null
  );

  let pageText = null;
  if (result.content) {
    pageText = el("details", { class: "page-text" },
      el("summary", {}, `Page text (${number.format(result.content.length)} characters)`),
      el("div", {}, result.content)
    );
  }

  return el("li", { class: "result", "data-state": status.state },
    head,
    el("div", { class: "result-body" }, title, ...excerpts, budget, pageText)
  );
}

function renderResults(data, body, elapsedMs) {
  renderSummary(data, body, elapsedMs);
  if (!data.results.length) {
    resultList.replaceChildren(el("li", { class: "no-results" },
      "No results. Try fewer words, a wider time range, or remove site filters."));
    return;
  }
  const terms = termsFrom(`${body.query} ${body.objective || ""}`);
  resultList.replaceChildren(...data.results.map((r, i) => renderResult(r, i, terms, body.max_chars_per_result)));
}

function shellQuote(text) {
  return `'${text.replace(/'/g, "'\\''")}'`;
}

function curlFor(body) {
  return [
    `curl -s ${location.origin}/v1/search \\`,
    `  -H "Authorization: Bearer $KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d ${shellQuote(JSON.stringify(body, null, 2))}`,
  ].join("\n");
}

function renderRequest(body, data) {
  lastRequestBody = body;
  lastResponse = data;
  $("curl").textContent = curlFor(body);
  $("raw").textContent = data ? JSON.stringify(data, null, 2) : "(no response body)";
  requestBox.hidden = false;
}

// ---- usage pill ------------------------------------------------------------------

async function refreshUsage() {
  const box = $("usage");
  if (!currentKey()) {
    box.hidden = true;
    return;
  }
  try {
    const resp = await fetch("/v1/usage", { headers: authHeaders() });
    if (!resp.ok) {
      box.hidden = true;
      return;
    }
    const usage = await resp.json();
    const share = usage.credit_limit ? Math.min(1, usage.credits_used / usage.credit_limit) : 0;
    $("usage-used").textContent = number.format(usage.credits_used);
    $("usage-of").textContent = `of ${number.format(usage.credit_limit)} credits`;
    $("usage-fill").style.width = `${(share * 100).toFixed(1)}%`;
    box.classList.toggle("high", share >= 0.8);
    box.title = `${usage.key_name}: ${number.format(usage.credits_used)} of ${number.format(usage.credit_limit)} credits used this month`;
    box.hidden = false;
  } catch {
    box.hidden = true;
  }
}

// ---- search ----------------------------------------------------------------

async function runSearch() {
  hideError();
  if (!currentKey()) {
    showKeyEntry();
    showError("Enter an API key to search.", "Create one with lookout create-key, or ask an admin.");
    keyInput.focus();
    return;
  }
  if (!queryInput.value.trim()) {
    showError("Enter a query to search.");
    queryInput.focus();
    return;
  }

  const body = readForm();
  if (inFlight) inFlight.abort();
  const controller = new AbortController();
  inFlight = controller;

  const started = performance.now();
  submitButton.disabled = true;
  const timer = setInterval(() => {
    submitText.textContent = `Searching… ${seconds(performance.now() - started)}`;
  }, 100);

  try {
    const resp = await fetch("/v1/search", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const data = await resp.json().catch(() => null);
    const elapsed = performance.now() - started;
    emptyState.hidden = true;
    renderRequest(body, data);
    if (!resp.ok || !data) {
      clearResults();
      explainFailure(resp.status, data);
      return;
    }
    renderResults(data, body, elapsed);
    refreshUsage();
    // On narrow screens results sit below the form; bring them into view.
    if (window.matchMedia("(max-width: 820px)").matches) {
      const smooth = window.matchMedia("(prefers-reduced-motion: no-preference)").matches;
      summary.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
    }
  } catch (error) {
    if (error.name === "AbortError") return;
    clearResults();
    showError("Could not reach the API.", "Check that the server is running at this address.");
  } finally {
    clearInterval(timer);
    if (inFlight === controller) {
      submitText.textContent = "Search";
      submitButton.disabled = false;
      inFlight = null;
    }
  }
}

async function copyText(button, text, label) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Copy failed";
  }
  setTimeout(() => { button.textContent = label; }, 1800);
}

// ---- wiring ------------------------------------------------------------------

form.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    runSearch();
  }
});

for (const radio of form.querySelectorAll('input[name="mode"]')) {
  radio.addEventListener("change", syncModeOptions);
}

let usageTimer = null;
keyInput.addEventListener("input", () => {
  saveKey(currentKey());
  $("usage").hidden = true; // never show the previous key's usage against a new key
  clearTimeout(usageTimer);
  usageTimer = setTimeout(refreshUsage, 400);
});

keyForm.addEventListener("submit", (event) => {
  event.preventDefault();
  saveKey(currentKey());
  showKeyPill();
  refreshUsage();
});

$("change-key").addEventListener("click", () => {
  showKeyEntry();
  keyInput.focus();
  keyInput.select();
});

for (const button of document.querySelectorAll(".example")) {
  button.addEventListener("click", () => {
    queryInput.value = button.dataset.query;
    objectiveInput.value = button.dataset.objective || "";
    const radio = form.querySelector(`input[name="mode"][value="${button.dataset.mode}"]`);
    if (radio) radio.checked = true;
    syncModeOptions();
    runSearch();
  });
}

$("copy-curl").addEventListener("click", (event) => {
  copyText(event.currentTarget, lastRequestBody && curlFor(lastRequestBody), "Copy as curl");
});

$("copy-json").addEventListener("click", (event) => {
  copyText(event.currentTarget, lastResponse && JSON.stringify(lastResponse, null, 2), "Copy JSON");
});

$("request-toggle").addEventListener("click", (event) => {
  const button = event.currentTarget;
  const open = button.getAttribute("aria-expanded") !== "true";
  button.setAttribute("aria-expanded", String(open));
  $("request-body").hidden = !open;
});

keyInput.value = loadKey();
if (keyInput.value) showKeyPill();
syncModeOptions();
refreshUsage();

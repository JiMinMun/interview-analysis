/* Every quote in the library as a list, for closed coding.
 *
 * Closed coding is one pass down the quotes with a fixed codebook beside them:
 * read a quote, apply the codes that fit, move on. The reader's sidebar is too
 * narrow for that and the themes page is about arranging, not labelling, so this
 * page is only the quotes and their tags.
 *
 * Two decisions shape it:
 *
 *   - Tagging a quote never re-filters the list. Working through "untagged" would
 *     otherwise pull each quote out from under the cursor the moment it got a
 *     code. The list only changes when a filter does.
 *   - The codebook keeps a stable alphabetical order, so the number on a code
 *     stays the same while you work instead of reshuffling as its count grows.
 *
 * Codes are study-wide: renaming or deleting one here changes it on every quote
 * in every recording, which is the only way a codebook can be revised without
 * splitting a code in two.
 */

import { $, api, escapeHtml, formatTime, recall, remember } from "./util.js";
import { applyStoredTheme, bindThemeToggle, notify } from "./chrome.js";
import { mountTagFields } from "./tagfield.js";

//: Codes added here before any quote carries them. A tag only becomes part of
//: the library's vocabulary once a quote file records it, so a codebook typed up
//: front would otherwise vanish on reload.
const PENDING_KEY = "subtitle-search:pending-codes";

const el = {
  meta: $("meta"),
  notices: $("notices"),
  search: $("search-input"),
  filterTag: $("filter-tag"),
  filterRecording: $("filter-recording"),
  filterSpeaker: $("filter-speaker"),
  groupBy: $("group-by"),
  codeFilter: $("code-filter"),
  summary: $("summary"),
  list: $("list"),
  codebook: $("codebook"),
  codebookCount: $("codebook-count"),
  codebookAdd: $("codebook-add"),
  codebookInput: $("codebook-input"),
};

const state = {
  quotes: [],
  byRef: new Map(),
  recordings: new Map(),
  //: Tags the library remembers, whether or not anything carries them now.
  history: new Set(),
  pending: new Set(),
  //: Codes a quote must carry every one of to be shown -- how quotes sharing a
  //: set of codes are pulled together from across the interviews.
  required: new Set(),
  visible: [],
  active: null,
};

/* ------------------------------------------------------------- loading -- */

async function load() {
  const library = await api("/api/library");
  state.recordings = new Map(library.recordings.map((r) => [r.id, r]));
  await loadQuotes();

  fillFilters();
  const params = new URLSearchParams(location.search);
  const wanted = params.get("recording");
  if (wanted && state.recordings.has(wanted)) el.filterRecording.value = wanted;
  for (const code of params.getAll("code")) state.required.add(code);

  renderCodeFilter();
  renderList();
}

/** The quotes and the vocabulary, which a rename or delete changes everywhere. */
async function loadQuotes() {
  const [quotes, vocabulary] = await Promise.all([
    api("/api/library/quotes"),
    api("/api/library/vocabulary"),
  ]);
  state.quotes = quotes.quotes;
  state.byRef = new Map(state.quotes.map((q) => [q.ref, q]));
  state.history = new Set(vocabulary.tags.map((entry) => entry.tag));
  state.pending = new Set(readPending().filter((tag) => !state.history.has(tag)));
  savePending();
}

function readPending() {
  try {
    const parsed = JSON.parse(recall(PENDING_KEY, "[]"));
    return Array.isArray(parsed) ? parsed.filter((t) => typeof t === "string") : [];
  } catch (_) {
    return [];
  }
}

function savePending() {
  remember(PENDING_KEY, JSON.stringify([...state.pending]));
}

/* ------------------------------------------------------------ codebook -- */

/** Every code with how widely it is used, in a fixed order. */
function codebook() {
  const entries = new Map();
  const entry = (tag) => {
    if (!entries.has(tag)) {
      entries.set(tag, { tag, quote_count: 0, recording_count: 0, recordings: new Set() });
    }
    return entries.get(tag);
  };
  for (const tag of state.history) entry(tag);
  for (const tag of state.pending) entry(tag);
  for (const quote of state.quotes) {
    for (const tag of quote.tags || []) {
      const item = entry(tag);
      item.quote_count += 1;
      item.recordings.add(quote.recording_id);
    }
  }
  return [...entries.values()]
    .map((item) => ({ ...item, recording_count: item.recordings.size }))
    .sort((a, b) => a.tag.localeCompare(b.tag, undefined, { sensitivity: "base" }));
}

function renderCodebook() {
  const codes = codebook();
  const active = state.byRef.get(state.active);
  const on = new Set(active?.tags || []);
  el.codebookCount.textContent = codes.length ? `${codes.length} codes` : "";

  if (!codes.length) {
    el.codebook.innerHTML =
      '<p class="empty">No codes yet. Add your codebook above, or type a tag on any quote.</p>';
    return;
  }

  el.codebook.innerHTML = codes
    .map((code, index) => {
      const tag = escapeHtml(code.tag);
      return `
      <div class="code-row" data-tag="${tag}">
        <button class="code" type="button"
                aria-pressed="${on.has(code.tag)}" ${active ? "" : "disabled"}>
          <kbd class="code__key">${index < 9 ? index + 1 : ""}</kbd>
          <span class="code__name">${tag}</span>
          <span class="code__count" title="${code.quote_count} quotes in ${code.recording_count} recordings">${code.quote_count}</span>
        </button>
        <span class="code-row__tools">
          <button class="icon-btn" type="button" data-action="find"
                  aria-pressed="${state.required.has(code.tag)}"
                  title="Show only quotes with this code, from every recording">⌕</button>
          <button class="icon-btn" type="button" data-action="rename" title="Rename this code everywhere">✎</button>
          <button class="icon-btn" type="button" data-action="delete" title="Delete this code from every quote">×</button>
        </span>
      </div>`;
    })
    .join("");
}

el.codebook.addEventListener("click", (event) => {
  const row = event.target.closest(".code-row");
  if (!row) return;
  const tag = row.dataset.tag;
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "find") toggleRequired(tag);
  else if (action === "rename") renameCode(tag);
  else if (action === "delete") deleteCode(tag);
  else if (event.target.closest(".code")) toggleCode(tag);
});

/** How far a code reaches, for the rename and delete prompts. */
function reach(tag) {
  const code = codebook().find((c) => c.tag === tag);
  if (!code || !code.quote_count) return "No quote carries it yet.";
  const quotes = code.quote_count === 1 ? "1 quote" : `${code.quote_count} quotes`;
  const recordings = code.recording_count === 1 ? "1 recording" : `${code.recording_count} recordings`;
  return `It is on ${quotes} in ${recordings}.`;
}

async function renameCode(tag) {
  const answer = prompt(
    `Rename "${tag}" on every quote. ${reach(tag)}\n` +
      "Renaming onto a code that already exists merges the two.",
    tag
  );
  const next = (answer || "").trim().replace(/\s+/g, " ");
  if (!next || next === tag) return;

  if (state.history.has(tag)) {
    try {
      await api("/api/library/tags/rename", { method: "POST", body: { from: tag, to: next } });
    } catch (error) {
      notify(el.notices, `Could not rename that code: ${error.message}`, { kind: "warn" });
      return;
    }
  }
  if (state.pending.delete(tag)) {
    if (!state.history.has(next)) state.pending.add(next);
    savePending();
  }
  if (state.required.delete(tag)) state.required.add(next);
  await refresh();
}

async function deleteCode(tag) {
  if (!confirm(`Delete "${tag}" from every quote? ${reach(tag)}\nThe quotes themselves are kept.`)) {
    return;
  }
  if (state.history.has(tag)) {
    try {
      await api("/api/library/tags/delete", { method: "POST", body: { tag } });
    } catch (error) {
      notify(el.notices, `Could not delete that code: ${error.message}`, { kind: "warn" });
      return;
    }
  }
  if (state.pending.delete(tag)) savePending();
  state.required.delete(tag);
  await refresh();
}

/** Re-read everything after a change that reached quotes outside this page's view. */
async function refresh() {
  try {
    await loadQuotes();
  } catch (error) {
    notify(el.notices, `Could not reload the quotes: ${error.message}`, { kind: "warn" });
  }
  fillTagFilter();
  renderCodeFilter();
  renderList();
}

/* ------------------------------------------------- codes shared across -- */

function toggleRequired(tag) {
  if (!state.required.delete(tag)) state.required.add(tag);
  // "Untagged" and "carries these codes" cannot both hold.
  if (state.required.size && el.filterTag.value === "__untagged") el.filterTag.value = "";
  renderCodeFilter();
  renderList();
}

function renderCodeFilter() {
  const codes = [...state.required];
  el.codeFilter.hidden = !codes.length;
  el.codeFilter.innerHTML = codes.length
    ? `<span>With ${codes.length > 1 ? "all of" : ""}</span>` +
      codes
        .map(
          (tag) => `<span class="chip">${escapeHtml(tag)}<button type="button" class="chip__x"
                     data-tag="${escapeHtml(tag)}" aria-label="Stop filtering by ${escapeHtml(tag)}">✕</button></span>`
        )
        .join("") +
      `<button class="icon-btn" type="button" data-clear>clear</button>`
    : "";
}

el.codeFilter.addEventListener("click", (event) => {
  if (event.target.closest("[data-clear]")) {
    state.required.clear();
  } else {
    const x = event.target.closest(".chip__x");
    if (!x) return;
    state.required.delete(x.dataset.tag);
  }
  renderCodeFilter();
  renderList();
});

el.codebookAdd.addEventListener("submit", (event) => {
  event.preventDefault();
  const tag = el.codebookInput.value.trim().replace(/\s+/g, " ");
  el.codebookInput.value = "";
  if (!tag) return;
  const known = codebook().some((code) => code.tag.toLowerCase() === tag.toLowerCase());
  if (!known) {
    state.pending.add(tag);
    savePending();
  }
  renderCodebook();
  fillTagFilter();
});

/** Put a code on the selected quote, or take it off. */
async function toggleCode(tag) {
  const quote = state.byRef.get(state.active);
  if (!quote) return;
  const tags = quote.tags || [];
  const next = tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag];
  await saveTags(quote, next);
  remountTagField(quote.ref);
}

/* ---------------------------------------------------------------- list -- */

function fillFilters() {
  fillTagFilter();

  const recordings = [...state.recordings.values()];
  el.filterRecording.innerHTML =
    `<option value="">every recording</option>` +
    recordings
      .map((r) => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.title)}</option>`)
      .join("");

  const speakers = [...new Set(state.quotes.map((q) => q.speaker).filter(Boolean))].sort();
  el.filterSpeaker.innerHTML =
    `<option value="">anyone</option>` +
    speakers.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
}

/**
 * The tag filter follows the codebook, so it is rebuilt when codes change.
 *
 * Picking a code here adds it to the codes a quote must carry rather than
 * replacing the last one, so two or three codes can be narrowed to the quotes
 * that share all of them.
 */
function fillTagFilter() {
  const current = el.filterTag.value;
  const codes = codebook();
  el.filterTag.innerHTML =
    `<option value="">any</option>
     <option value="__untagged">untagged</option>
     <option value="__tagged">tagged</option>` +
    (codes.length
      ? `<optgroup label="also with code…">` +
        codes
          .map((code) => `<option value="${escapeHtml(code.tag)}">${escapeHtml(code.tag)}</option>`)
          .join("") +
        `</optgroup>`
      : "");
  el.filterTag.value = current;
  if (el.filterTag.value !== current) el.filterTag.value = "";
}

function matches(quote) {
  const tags = quote.tags || [];
  const tag = el.filterTag.value;
  if (tag === "__untagged" && tags.length) return false;
  if (tag === "__tagged" && !tags.length) return false;
  for (const code of state.required) if (!tags.includes(code)) return false;
  if (el.filterRecording.value && quote.recording_id !== el.filterRecording.value) return false;
  if (el.filterSpeaker.value && quote.speaker !== el.filterSpeaker.value) return false;

  const needle = el.search.value.trim().toLowerCase();
  if (!needle) return true;
  return [quote.text, quote.note, quote.speaker, ...tags]
    .some((value) => (value || "").toLowerCase().includes(needle));
}

/**
 * Recordings in library order, then time within each -- the order they were said.
 * Grouped by speaker, each person's quotes come together first, so j / k walks
 * the list in the order it is drawn.
 */
function ordered(quotes) {
  const rank = new Map([...state.recordings.keys()].map((id, index) => [id, index]));
  const bySpeaker = el.groupBy.value === "speaker";
  return [...quotes].sort(
    (a, b) =>
      (bySpeaker ? (a.speaker || "").localeCompare(b.speaker || "") : 0) ||
      (rank.get(a.recording_id) ?? 0) - (rank.get(b.recording_id) ?? 0) ||
      a.start_time - b.start_time
  );
}

function recordingTitle(id) {
  return state.recordings.get(id)?.title ?? id;
}

/** The heading a quote falls under, or null when the list is not grouped. */
function groupOf(quote) {
  if (el.groupBy.value === "recording") return recordingTitle(quote.recording_id);
  if (el.groupBy.value === "speaker") return quote.speaker || "Unknown speaker";
  return null;
}

/** The rows, with a heading wherever the group changes. */
function rows(quotes) {
  const counts = new Map();
  for (const quote of quotes) counts.set(groupOf(quote), (counts.get(groupOf(quote)) || 0) + 1);
  let last;
  return quotes
    .map((quote) => {
      const group = groupOf(quote);
      const head =
        group !== null && group !== last
          ? `<h3 class="coding__group">${escapeHtml(group)}<span>${counts.get(group)}</span></h3>`
          : "";
      last = group;
      return head + row(quote);
    })
    .join("");
}

function row(quote) {
  const recording = state.recordings.get(quote.recording_id);
  return `
    <article class="coderow qcard--${quote.color || "amber"}" data-ref="${escapeHtml(quote.ref)}">
      <div class="coderow__quote">
        <p class="coderow__text">${escapeHtml(quote.text)}</p>
        <div class="qcard__meta">
          <span class="quote__speaker">${escapeHtml(quote.speaker || "—")}</span>
          <span class="qcard__where">${escapeHtml(recording ? recording.title : quote.recording_id)}</span>
          <time>${formatTime(quote.start_time)}</time>
          <a class="icon-btn" target="_blank" rel="noopener"
             href="/reader?recording=${encodeURIComponent(quote.recording_id)}&t=${quote.start_time}"
             title="Open in the transcript, in a new tab">↗</a>
        </div>
        ${quote.note ? `<p class="qcard__note">${escapeHtml(quote.note)}</p>` : ""}
      </div>
      <div class="tagfield" data-id="${escapeHtml(quote.ref)}"></div>
    </article>`;
}

function renderList() {
  state.visible = ordered(state.quotes.filter(matches));
  if (!state.visible.some((q) => q.ref === state.active)) {
    state.active = state.visible[0]?.ref ?? null;
  }
  renderSummary();

  if (!state.visible.length) {
    el.list.innerHTML = state.quotes.length
      ? '<p class="empty">No quotes match these filters.</p>'
      : '<p class="empty">No quotes saved yet. Select text in a transcript to save one.</p>';
    renderCodebook();
    return;
  }

  el.list.innerHTML = rows(state.visible);
  mountRows(el.list);
  markActive();
  renderCodebook();
}

function mountRows(root) {
  mountTagFields(root, {
    getTags: (ref) => state.byRef.get(ref)?.tags || [],
    getVocabulary: codebook,
    onCommit: (ref, tags) => {
      const quote = state.byRef.get(ref);
      if (quote) saveTags(quote, tags);
    },
  });
}

/** Rebuild one row's tag field after its tags changed from outside it. */
function remountTagField(ref) {
  const card = rowFor(ref);
  const host = card?.querySelector(".tagfield");
  if (!host) return;
  const focused = host.contains(document.activeElement);
  host.removeAttribute("data-ready");
  host.innerHTML = "";
  mountRows(card);
  if (focused) host.querySelector(".tagfield__input")?.focus();
}

function rowFor(ref) {
  return [...el.list.querySelectorAll(".coderow")].find((node) => node.dataset.ref === ref);
}

function renderSummary() {
  const untagged = state.quotes.filter((q) => !(q.tags || []).length).length;
  // With codes chosen, how widely they are shared is the point: one person
  // saying it twenty times is not a finding, twelve people saying it is.
  const spread = state.required.size
    ? [
        plural(new Set(state.visible.map((q) => q.recording_id)).size, "recording"),
        plural(new Set(state.visible.map((q) => q.speaker).filter(Boolean)).size, "speaker"),
      ].join(", ")
    : null;
  el.summary.textContent = [
    `${state.visible.length} of ${state.quotes.length} shown`,
    spread && `from ${spread}`,
    untagged ? `${untagged} untagged` : "every quote tagged",
  ]
    .filter(Boolean)
    .join("  ·  ");
  el.meta.textContent = `${state.recordings.size} recordings  ·  ${state.quotes.length} quotes`;
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

async function saveTags(quote, tags) {
  try {
    const { highlight } = await api(
      `/api/recordings/${encodeURIComponent(quote.recording_id)}/highlights/${encodeURIComponent(quote.id)}`,
      { method: "PATCH", body: { tags } }
    );
    quote.tags = highlight.tags || [];
    for (const tag of quote.tags) {
      state.history.add(tag);
      if (state.pending.delete(tag)) savePending();
    }
  } catch (error) {
    notify(el.notices, `Could not save those tags: ${error.message}`, { kind: "warn" });
    // The field already shows the change; put it back to what is on disk.
    remountTagField(quote.ref);
  }
  renderSummary();
  renderCodebook();
  fillTagFilter();
}

/* ----------------------------------------------------------- selection -- */

function markActive() {
  for (const node of el.list.querySelectorAll(".coderow")) {
    node.classList.toggle("coderow--active", node.dataset.ref === state.active);
  }
}

function select(ref, { scroll = false } = {}) {
  if (!ref || ref === state.active) return;
  state.active = ref;
  markActive();
  renderCodebook();
  if (scroll) rowFor(ref)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function step(delta) {
  const index = state.visible.findIndex((q) => q.ref === state.active);
  const next = state.visible[Math.max(0, Math.min(state.visible.length - 1, index + delta))];
  if (next) select(next.ref, { scroll: true });
}

// Focus and clicks both select, so tabbing into a tag field moves the codebook
// to that quote too.
el.list.addEventListener("focusin", (event) => {
  const card = event.target.closest(".coderow");
  if (card) select(card.dataset.ref);
});

el.list.addEventListener("click", (event) => {
  const card = event.target.closest(".coderow");
  if (card) select(card.dataset.ref);
});

const TYPING = new Set(["INPUT", "TEXTAREA", "SELECT"]);

document.addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;

  if (TYPING.has(event.target.tagName)) {
    // Escape leaves a field so the number keys work again.
    if (event.key === "Escape" && !event.defaultPrevented) event.target.blur();
    return;
  }

  if (event.key === "j" || event.key === "ArrowDown") {
    event.preventDefault();
    step(1);
  } else if (event.key === "k" || event.key === "ArrowUp") {
    event.preventDefault();
    step(-1);
  } else if (event.key === "/") {
    event.preventDefault();
    el.search.focus();
  } else if (event.key === "t") {
    const input = rowFor(state.active)?.querySelector(".tagfield__input");
    if (input) {
      event.preventDefault();
      input.focus();
    }
  } else if (/^[1-9]$/.test(event.key)) {
    const code = codebook()[Number(event.key) - 1];
    if (code) {
      event.preventDefault();
      toggleCode(code.tag);
    }
  }
});

/* ------------------------------------------------------------- filters -- */

for (const control of [el.filterRecording, el.filterSpeaker, el.groupBy]) {
  control.addEventListener("change", renderList);
}

el.filterTag.addEventListener("change", () => {
  const value = el.filterTag.value;
  if (value && !value.startsWith("__")) {
    el.filterTag.value = "";
    toggleRequired(value);
    return;
  }
  if (value === "__untagged" && state.required.size) {
    state.required.clear();
    renderCodeFilter();
  }
  renderList();
});

let searchTimer;
el.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(renderList, 150);
});

/* ---------------------------------------------------------------- start -- */

applyStoredTheme();
bindThemeToggle($("theme-toggle"));
load().catch((error) =>
  notify(el.notices, `Could not load the quotes: ${error.message}`, { kind: "warn" })
);

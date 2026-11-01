/* Wiring, shared state, and the reading/following cursor state machine.
 *
 * Reading is the default and the point of the tool. The transcript starts in
 * READING mode with the dock minimized: the cursor follows where you are in the
 * text and quietly cues the player to match, so playback always starts where you
 * are looking. Pressing play switches to FOLLOWING, where the transcript keeps up
 * with the audio instead. Scrolling or moving the cursor by hand drops back to
 * READING without stopping playback, and a "Follow along" button re-attaches.
 */

import { $, api, escapeHtml, formatTime, recall, remember } from "./util.js";
import {
  applyHighlights,
  cacheGeometry,
  expandChunks,
  chunkIndexAtScroll,
  chunkIndexAtTime,
  flashCue,
  renderTranscript,
  setCursor,
  updateSpine,
} from "./transcript.js";
import { cue, initPlayer, nudge, seekAndPlay, stepRate, togglePlay } from "./player.js";
import { initSearch } from "./search.js";
import { copySelection, hideQuoteBar, initHighlights, renderList, save } from "./highlights.js";
import { enterEdit, exitEdit, isEditing, splitAtWord } from "./editing.js";
import { notify, working } from "./chrome.js";

const ctx = {
  el: {
    reader: $("reader"),
    transcript: $("transcript"),
    chunks: $("chunks"),
    spineMarker: $("spine-marker"),
    spineDot: $("spine-dot"),
    title: $("title"),
    meta: $("meta"),
    notices: $("notices"),
    roster: $("roster"),
    timing: $("timing"),
    sidebar: $("sidebar"),
    panelSearch: $("panel-search"),
    panelHighlights: $("panel-highlights"),
    tabSearch: $("tab-search"),
    tabHighlights: $("tab-highlights"),
    themeToggle: $("theme-toggle"),
    libraryLink: $("library-link"),
    searchInput: $("search-input"),
    regexToggle: $("regex-toggle"),
    searchResults: $("search-results"),
    searchCount: $("search-count"),
    highlightList: $("highlight-list"),
    highlightCount: $("highlight-count"),
    highlightsPath: $("highlights-path"),
    tagFilters: $("tag-filters"),
    sidebarExpand: $("sidebar-expand"),
    codeLink: $("code-link"),
    dock: $("dock"),
    dockToggle: $("dock-toggle"),
    dockGrip: $("dock-grip"),
    dockStage: $("dock-stage"),
    media: $("media"),
    play: $("play"),
    scrub: $("scrub"),
    clock: $("clock"),
    duration: $("duration"),
    rate: $("rate"),
    follow: $("follow"),
    quotebar: $("quotebar"),
    quotebarTime: $("quotebar-time"),
    quotebarColors: $("quotebar-colors"),
    quotebarNote: $("quotebar-note"),
    quotebarCopy: $("quotebar-copy"),
    quotebarHand: $("quotebar-hand"),
  },
  mode: "reading",
  cursorIndex: 0,
  currentTime: 0,
  chunks: [],
  // Blocks as the server grouped them, before any are broken open.
  serverChunks: [],
  splitCues: new Set(),
  cueById: new Map(),
  cueByIndex: new Map(),
  highlights: [],
  knownTags: [],
  // Every tag used anywhere in the library, for completing as you type.
  vocabulary: [],
  colors: ["amber"],
  paintedCues: new Set(),
  activeHighlightId: null,
  tagFilter: null,
  rosterEditing: null,
  pendingSelection: null,
  scrubbing: false,
};

/* ---------------------------------------------------------------- notices -- */

ctx.notify = (message, options) => notify(ctx.el.notices, message, options);
//: For work that takes seconds. Returns the function that takes the message away.
ctx.working = (message) => working(ctx.el.notices, message);

/* ------------------------------------------------------------------ tabs -- */

ctx.showTab = (which) => {
  const showSearch = which === "search";
  ctx.el.panelSearch.hidden = !showSearch;
  ctx.el.panelHighlights.hidden = showSearch;
  ctx.el.tabSearch.setAttribute("aria-pressed", String(showSearch));
  ctx.el.tabHighlights.setAttribute("aria-pressed", String(!showSearch));
};

/* -------------------------------------------------------- wide sidebar -- */

// Twenty-two rems is right for reading with the list alongside, and too narrow
// for a pass over the quotes themselves -- notes and tags wrap to a word a line.
// Widening trades transcript for list without leaving the page.
const SIDEBAR_WIDE_KEY = "subtitle-search:sidebar-wide";

function setSidebarWide(wide) {
  ctx.el.sidebar.classList.toggle("sidebar--wide", wide);
  ctx.el.sidebarExpand.setAttribute("aria-pressed", String(wide));
  ctx.el.sidebarExpand.textContent = wide ? "⇥" : "⇤";
  ctx.el.sidebarExpand.title = wide ? "Narrow the quotes panel" : "Widen the quotes panel";
  remember(SIDEBAR_WIDE_KEY, wide ? "1" : "0");
}

setSidebarWide(recall(SIDEBAR_WIDE_KEY) === "1");
ctx.el.sidebarExpand.addEventListener("click", () => {
  setSidebarWide(!ctx.el.sidebar.classList.contains("sidebar--wide"));
  // The transcript column just changed width, so every block reflowed.
  cacheGeometry(ctx);
  updateSpine(ctx);
});

/* ------------------------------------------------------------ mode logic -- */

ctx.setMode = (mode) => {
  if (ctx.mode === mode) return;
  ctx.mode = mode;
  syncFollowButton();
  updateSpine(ctx);
};

function syncFollowButton() {
  const playing = !ctx.el.media.paused && Boolean(ctx.el.media.src);
  ctx.el.follow.hidden = !(playing && ctx.mode === "reading");
}

/** Display blocks: the server's grouping, with any split ones broken open. */
function regroup(ctx) {
  ctx.chunks = expandChunks(ctx.serverChunks, ctx.cueById, ctx.splitCues);
}

/* ------------------------------------------------------------------ load -- */

async function load() {
  const config = await api("/api/config");
  ctx.colors = config.colors;
  // The library links straight to a recording, and to a moment inside it.
  const params = new URLSearchParams(location.search);
  const asked = params.get("recording");
  ctx.recordingId =
    (asked && config.recordings.some((r) => r.id === asked) && asked) ||
    config.default_recording_id;
  // The library is always the way back, however few recordings it holds.
  ctx.el.libraryLink.hidden = false;
  if (ctx.recordingId) {
    ctx.el.codeLink.href = `/quotes?recording=${encodeURIComponent(ctx.recordingId)}`;
  }
  if (!ctx.recordingId) {
    ctx.el.chunks.innerHTML = '<p class="empty">No recording loaded.</p>';
    return;
  }

  const data = await api(`/api/recordings/${ctx.recordingId}`);
  ctx.data = data;
  ctx.serverChunks = data.transcript.chunks;
  ctx.parts = data.transcript.parts || [];
  ctx.highlights = data.highlights;
  ctx.knownTags = data.known_tags;

  for (const cueItem of data.transcript.cues) {
    ctx.cueById.set(cueItem.id, cueItem);
    ctx.cueByIndex.set(cueItem.index, cueItem);
  }
  regroup(ctx);

  document.title = data.title;
  ctx.el.title.textContent = data.title;
  ctx.el.highlightsPath.textContent = data.highlights_file;

  const diagnostics = data.transcript.diagnostics;
  ctx.el.meta.textContent = [
    formatTime(data.duration),
    `${diagnostics.chunk_count} blocks`,
    `${diagnostics.speakers.length} speakers`,
    data.media_file || "no media",
  ].join("  ·  ");

  // Suggestions span the study, so a tag coined in one interview is offered in
  // every other one. Not fatal if it fails -- the field still takes free text.
  api("/api/library/vocabulary")
    .then((data) => {
      ctx.vocabulary = data.tags;
      renderList(ctx);
    })
    .catch(() => {});

  ctx.onHighlightsChanged = () => {
    applyHighlights(ctx);
    renderList(ctx);
  };

  /**
   * Take a rebuilt transcript back wholesale.
   *
   * Reassigning a speaker regroups every block after it, so patching in place
   * would mean reimplementing the chunker in the browser. Re-rendering and
   * returning to the line being worked on is both simpler and always right.
   */
  /**
   * The speaker keys: what they are, and where they are changed.
   *
   * This lives here rather than in the transcript because nobody should have to
   * open a .vtt to name the people in it. It doubles as a reminder of which key
   * is whom while reading, which is why it stays on screen.
   */
  ctx.renderRoster = () => {
    const roster = ctx.data?.transcript?.roster || [];
    // Offering the labels already in the file is right when there are several
    // of them -- they are the real speakers and just need keys. It is wrong when
    // there is one, because that label is the room rather than a person, and
    // rostering it would let its lines join and defeat labelling them apart.
    const found = ctx.data?.transcript?.speakers || [];
    const detected =
      found.length > 1 ? found.filter((name) => !roster.some((e) => e.name === name)) : [];
    ctx.el.roster.hidden = false;

    if (ctx.rosterEditing != null) {
      const entry = ctx.rosterEditing === "new" ? { key: "", name: "" } : roster[ctx.rosterEditing];
      ctx.el.roster.innerHTML =
        `<span class="roster__lead">${ctx.rosterEditing === "new" ? "new speaker" : "rename"}</span>` +
        `<input class="roster__field roster__field--key" id="roster-key" maxlength="1"
                value="${escapeAttr(entry.key || "")}" placeholder="key" aria-label="Key">` +
        `<input class="roster__field" id="roster-name" value="${escapeAttr(entry.name || "")}"
                placeholder="name" aria-label="Speaker name">` +
        `<button class="btn" id="roster-save" type="button">Save</button>` +
        `<button class="btn" id="roster-cancel" type="button">Cancel</button>`;
      const name = $("roster-name");
      name.focus();
      name.select();
      return;
    }

    ctx.el.roster.innerHTML =
      `<span class="roster__lead">assign this block</span>` +
      (roster.length
        ? roster
            .map(
              (entry, index) =>
                `<span class="roster__who">
                   <button class="roster__hit" data-assign="${escapeAttr(entry.name)}"
                           title="Assign this block to ${escapeAttr(entry.name)}">
                     <kbd>${escapeAttr(entry.key || "·")}</kbd>${escapeAttr(entry.name)}</button>
                   <button class="roster__edit" data-edit="${index}" aria-label="Rename ${escapeAttr(entry.name)}">✎</button>
                   <button class="roster__edit" data-drop="${index}" aria-label="Remove ${escapeAttr(entry.name)}">✕</button>
                 </span>`
            )
            .join("")
        : `<span class="roster__empty">${found.length > 1
            ? "nobody named yet — add the speakers to label with a keypress"
            : "one label covers everyone here — add who was actually in the room"}</span>`) +
      `<button class="btn" data-add="1" type="button">+ Speaker</button>` +
      detected
        .map(
          (name) =>
            `<button class="roster__suggest" data-quick="${escapeAttr(name)}"
                     title="Add ${escapeAttr(name)} to the roster">+ ${escapeAttr(name)}</button>`
        )
        .join("");
  };

  /* ------------------------------------------------------ word timings -- */

  /* Times inside a caption are interpolated until the words have been aligned to
   * the audio, which is wrong by about the length of any pause the speaker took.
   * This strip says which of the two you are looking at and offers to fix it.
   *
   * Alignment is not automatic: it reads the audio and costs real seconds, so it
   * is a thing you ask for. Splitting a caption asks for that one caption by
   * itself, because a cut is where a guess does the most damage. */
  ctx.alignState = { available: false, reason: "", running: false, done: 0, total: 0 };

  ctx.renderTiming = () => {
    const state = ctx.alignState;
    const cover = ctx.data?.timing_coverage || { timed: 0, total: 0, complete: false };
    // Nothing to offer and nothing measured: stay out of the way entirely.
    if (!state.available && !cover.timed) {
      ctx.el.timing.hidden = true;
      return;
    }
    ctx.el.timing.hidden = false;

    if (state.running) {
      const pct = state.total ? Math.round((state.done / state.total) * 100) : 0;
      ctx.el.timing.innerHTML =
        `<span class="roster__lead">timings</span>` +
        `<span class="timing__bar"><span class="timing__fill" style="width:${pct}%"></span></span>` +
        `<span class="timing__state">measuring — ${state.done} of ${state.total} captions</span>` +
        `<button class="btn" data-align-stop="1" type="button">Stop</button>`;
      return;
    }

    const measured = `${cover.timed} of ${cover.total} captions measured`;
    ctx.el.timing.innerHTML =
      `<span class="roster__lead">timings</span>` +
      `<span class="timing__state">${cover.complete ? "aligned to the audio" : measured}</span>` +
      (state.available && !cover.complete
        ? `<button class="btn" data-align="1" type="button">${cover.timed ? "Measure the rest" : "Measure word timings"}</button>`
        : "") +
      (state.available
        ? ""
        : `<span class="timing__why">${escapeHtml(state.reason)}</span>`);
  };

  /** Walk the session in batches, so there is progress to show and a way out. */
  ctx.runAlignment = async () => {
    const state = ctx.alignState;
    if (state.running) return;
    const cover = ctx.data?.timing_coverage || { timed: 0, total: 0 };
    Object.assign(state, {
      running: true,
      stop: false,
      done: cover.timed || 0,
      total: cover.total || 0,
    });
    ctx.renderTiming();

    try {
      while (!state.stop) {
        const before = state.done;
        const result = await api(`/api/recordings/${ctx.recordingId}/align`, {
          method: "POST",
          body: {},
        });
        state.done = result.coverage.timed;
        state.total = result.coverage.total;
        ctx.data.timing_coverage = result.coverage;
        ctx.renderTiming();
        if (!result.remaining || !result.captions) break;
        // A batch that handed back captions but measured none of them would
        // otherwise be asked for again forever -- a recording with no audio, or
        // captions the aligner cannot place. Stop on the first lack of progress.
        if (result.coverage.timed <= before) {
          ctx.notify(
            `Stopped at ${state.done} of ${state.total}: those captions could not be measured.`,
            { kind: "warn", key: null }
          );
          break;
        }
      }
      ctx.notify(
        state.stop
          ? `Stopped — ${state.done} of ${state.total} captions measured.`
          : "Word timings measured. Timestamps in this session are no longer estimates."
      );
    } catch (error) {
      ctx.notify(`Could not measure timings: ${error.message}`, { kind: "warn", key: null });
    } finally {
      state.running = false;
      // The cues themselves changed server-side, so take the session back to
      // pick up which captions are now measured.
      try {
        ctx.onTranscriptChanged(await api(`/api/recordings/${ctx.recordingId}`));
      } catch (error) {
        ctx.renderTiming();
      }
    }
  };

  ctx.el.timing.addEventListener("click", (event) => {
    if (event.target.closest("[data-align]")) return ctx.runAlignment();
    if (event.target.closest("[data-align-stop]")) ctx.alignState.stop = true;
  });

  const escapeAttr = (value) => String(value).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /** Send the whole roster back; the server is the one that validates it. */
  ctx.saveRoster = async (entries) => {
    try {
      const result = await api(`/api/recordings/${ctx.recordingId}/roster`, {
        method: "PUT",
        body: { speakers: entries },
      });
      ctx.rosterEditing = null;
      ctx.onTranscriptChanged(result.recording);
    } catch (error) {
      ctx.notify(`Could not save the speakers: ${error.message}`, { kind: "warn", key: null });
    }
  };

  ctx.el.roster.addEventListener("click", (event) => {
    const roster = [...(ctx.data?.transcript?.roster || [])];
    const target = event.target.closest("[data-assign],[data-edit],[data-drop],[data-add],[data-quick]");
    if (!target) return;

    if (target.dataset.assign) return assignSpeaker(ctx, target.dataset.assign);
    if (target.dataset.quick) {
      return ctx.saveRoster([...roster, { key: null, name: target.dataset.quick }]);
    }
    if (target.dataset.drop) {
      roster.splice(Number(target.dataset.drop), 1);
      return ctx.saveRoster(roster);
    }
    if (target.dataset.edit != null) {
      ctx.rosterEditing = Number(target.dataset.edit);
      return ctx.renderRoster();
    }
    if (target.dataset.add) {
      ctx.rosterEditing = "new";
      return ctx.renderRoster();
    }
  });

  ctx.el.roster.addEventListener("keydown", (event) => {
    if (!event.target.closest(".roster__field")) return;
    event.stopPropagation();  // digits here name a speaker, they do not assign one
    if (event.key === "Enter") $("roster-save")?.click();
    if (event.key === "Escape") $("roster-cancel")?.click();
  });

  ctx.el.roster.addEventListener("click", (event) => {
    if (event.target.id === "roster-cancel") {
      ctx.rosterEditing = null;
      ctx.renderRoster();
    }
    if (event.target.id === "roster-save") {
      const roster = [...(ctx.data?.transcript?.roster || [])];
      const entry = { key: $("roster-key").value.trim() || null, name: $("roster-name").value };
      if (ctx.rosterEditing === "new") roster.push(entry);
      else roster[ctx.rosterEditing] = entry;
      ctx.saveRoster(roster.filter((e) => e.name.trim()));
    }
  });

  ctx.onTranscriptChanged = (payload, { keepEditingCue } = {}) => {
    exitEdit(ctx);
    ctx.data = payload;
    ctx.serverChunks = payload.transcript.chunks;
    ctx.parts = payload.transcript.parts || [];
    ctx.highlights = payload.highlights;
    ctx.cueById = new Map();
    ctx.cueByIndex = new Map();
    for (const item of payload.transcript.cues) {
      ctx.cueById.set(item.id, item);
      ctx.cueByIndex.set(item.index, item);
    }
    ctx.paintedCues = new Set();
    regroup(ctx);

    renderTranscript(ctx);
    applyHighlights(ctx);
    renderList(ctx);

    ctx.renderRoster();
    ctx.renderTiming();
    const index = keepEditingCue
      ? ctx.chunks.findIndex((chunk) => chunk.cue_ids.includes(keepEditingCue))
      : ctx.cursorIndex;
    setCursor(ctx, Math.max(0, index), { scroll: true });
    if (keepEditingCue && index >= 0) enterEdit(ctx, index);
  };

  renderTranscript(ctx);
  applyHighlights(ctx);
  initPlayer(ctx);
  initSearch(ctx);
  initHighlights(ctx);
  renderList(ctx);
  // Quotes are the output of a reading session, so that is what the sidebar
  // opens on. Search is a keystroke away with `/`.
  ctx.showTab("highlights");
  ctx.renderRoster();
  setCursor(ctx, 0);

  // Asked once: whether this machine can measure timings at all, and why not.
  api(`/api/recordings/${ctx.recordingId}/alignment`)
    .then((state) => {
      Object.assign(ctx.alignState, { available: state.available, reason: state.reason });
      ctx.renderTiming();
    })
    .catch(() => {});

  // A link from the library or the themes board carries a moment with it.
  const at = Number(params.get("t"));
  if (Number.isFinite(at) && at > 0) {
    const index = chunkIndexAtTime(ctx, at);
    setCursor(ctx, index, { scroll: true });
    seekAndPlay(ctx, at);
    flashCue(ctx, ctx.chunks[index]?.cue_ids?.[0]);
  }
  showDiagnostics(data, diagnostics);
}

function showDiagnostics(data, diagnostics) {
  if (data.migrated_from) {
    ctx.notify(
      `Quotes from ${data.migrated_from} were moved into ${data.highlights_file}. The original file was left in place as a backup.`,
      { kind: "info", key: null }
    );
  }

  if (data.highlights_stale) {
    ctx.notify(
      `The transcript changed since these quotes were saved. Their timestamps may no longer line up.`,
      { kind: "warn", key: null }
    );
  }

  // Speaker detection is a heuristic, so say what it decided rather than letting
  // a misparse be discovered an hour into a reading session.
  const key = `subtitle-search:parsed:${data.transcript.sha256}`;
  const speakers = diagnostics.speakers;
  const parts = diagnostics.part_count > 1
    ? `${diagnostics.part_count} recordings joined into one timeline. `
    : "";
  const summary = speakers.length
    ? `${parts}${diagnostics.cue_count} cues grouped into ${diagnostics.chunk_count} blocks. Speakers: ${speakers.join(", ")}.`
    : `${parts}${diagnostics.cue_count} cues, no speakers detected — blocks were split on pauses instead.`;
  ctx.notify(summary, { kind: speakers.length ? "info" : "warn", key });
}

/* ------------------------------------------------- reading cursor driver -- */

let scrollQueued = false;

ctx.el.reader.addEventListener("scroll", () => {
  if (ctx.mode !== "reading" || scrollQueued) return;
  scrollQueued = true;
  requestAnimationFrame(() => {
    scrollQueued = false;
    setCursor(ctx, chunkIndexAtScroll(ctx));
  });
});

// Detaching on real input intent is more reliable than trying to tell a
// programmatic scroll from a human one after the fact.
for (const event of ["wheel", "touchmove"]) {
  ctx.el.reader.addEventListener(event, () => ctx.setMode("reading"), { passive: true });
}

let cueTimer;
ctx.onCursorMoved = (chunk) => {
  if (!chunk || ctx.mode !== "reading") return;
  clearTimeout(cueTimer);
  cueTimer = setTimeout(() => cue(ctx, chunk.start), 200);
};

ctx.onTimeUpdate = (seconds) => {
  syncFollowButton();
  if (ctx.mode !== "following") return;
  const index = chunkIndexAtTime(ctx, seconds);
  if (index !== ctx.cursorIndex) setCursor(ctx, index, { scroll: true });
  else updateSpine(ctx);
};

ctx.el.chunks.addEventListener("click", (event) => {
  const chunkEl = event.target.closest(".chunk");
  if (!chunkEl) return;
  const index = ctx.chunks.findIndex((chunk) => chunk.id === chunkEl.dataset.chunkId);
  if (index < 0) return;

  // Clicking outside the block being corrected finishes editing it.
  if (isEditing(ctx) && ctx.editingChunk !== index) exitEdit(ctx);
  if (event.target.closest(".cue-lines, .edit-bar")) return;

  const mark = event.target.closest("mark.hl");
  if (mark) {
    ctx.activeHighlightId = mark.dataset.highlightId;
    ctx.showTab("highlights");
    applyHighlights(ctx);
  }

  // Clicking the timestamp means "play from here"; clicking the text just moves
  // the reading cursor and cues the player without starting it.
  const fromTimestamp = Boolean(event.target.closest(".chunk__time"));
  ctx.setMode("reading");
  setCursor(ctx, index);
  if (fromTimestamp) seekAndPlay(ctx, ctx.chunks[index].start, { play: true });
});

/* Double-click a word to cut the caption in front of it.
 *
 * The block-then-edit-then-keystroke route is a lot of ceremony for "these two
 * sentences are two different people". Pointing at the first word of the second
 * turn is the whole gesture, and the cut goes in front of that word.
 *
 * It writes to the transcript, so the toast says what happened and where the
 * boundary landed. The original file is always recoverable as `_original`. */
ctx.el.chunks.addEventListener("dblclick", (event) => {
  // While correcting text, a double-click means what it always means: select a
  // word. Edit mode has its own split, on the caret.
  if (isEditing(ctx)) return;

  const cueEl = event.target.closest(".cue");
  if (!cueEl) return;

  // The double-click has already selected the word; its start is where the
  // browser thinks the word begins, which is exactly the point being asked for.
  const selection = window.getSelection();
  let node = null;
  let offset = 0;
  if (selection?.rangeCount) {
    const range = selection.getRangeAt(0);
    if (cueEl === range.startContainer || cueEl.contains(range.startContainer)) {
      node = range.startContainer;
      offset = range.startOffset;
    }
  }
  // Fallback for a double-click that selected nothing -- on punctuation, say.
  // The standard call first, then WebKit's older one.
  if (!node && document.caretPositionFromPoint) {
    const position = document.caretPositionFromPoint(event.clientX, event.clientY);
    if (position && cueEl.contains(position.offsetNode)) {
      node = position.offsetNode;
      offset = position.offset;
    }
  }
  if (!node && document.caretRangeFromPoint) {
    const range = document.caretRangeFromPoint(event.clientX, event.clientY);
    if (range && cueEl.contains(range.startContainer)) {
      node = range.startContainer;
      offset = range.startOffset;
    }
  }
  if (!node) return;

  event.preventDefault();
  hideQuoteBar(ctx);
  selection?.removeAllRanges();
  splitAtWord(ctx, cueEl, node, offset);
});

ctx.el.follow.addEventListener("click", () => {
  ctx.setMode("following");
  setCursor(ctx, chunkIndexAtTime(ctx, ctx.currentTime), { scroll: true });
});

window.addEventListener("resize", () => {
  cacheGeometry(ctx);
  updateSpine(ctx);
});

/**
 * Give the block at the cursor a speaker, then move to the next one.
 *
 * Whole block rather than one caption: the cursor sits on a block, and on a
 * single-speaker transcript every caption *is* a block, which is exactly the
 * case this exists for. Advancing afterwards is what makes a labelling pass a
 * run of keypresses rather than a click each time.
 */
async function assignSpeaker(ctx, speaker) {
  const chunk = ctx.chunks[ctx.cursorIndex];
  if (!chunk || !speaker) return;
  if (chunk.speaker === speaker) {
    setCursor(ctx, ctx.cursorIndex + 1, { scroll: true });
    return;
  }

  const cues = chunk.cue_ids;
  try {
    const result = await api(
      `/api/recordings/${ctx.recordingId}/cues/${cues[0]}/speaker`,
      { method: "PATCH", body: { speaker, through: cues[cues.length - 1] } }
    );
    if (result.backup_created) ctx.notify(`Original transcript saved as ${result.backup_created}.`);
    const wasAt = ctx.cursorIndex;
    ctx.onTranscriptChanged(result.recording);
    // Blocks may have merged, so step past the one this cue now belongs to.
    const now = ctx.chunks.findIndex((c) => c.cue_ids.includes(cues[cues.length - 1]));
    setCursor(ctx, (now < 0 ? wasAt : now) + 1, { scroll: true });
  } catch (error) {
    ctx.notify(`Could not assign that speaker: ${error.message}`, { kind: "warn", key: null });
  }
}

/* -------------------------------------------------------------- keyboard -- */

const TYPING = new Set(["INPUT", "TEXTAREA", "SELECT"]);

document.addEventListener("keydown", (event) => {
  if (TYPING.has(event.target.tagName) || event.target.isContentEditable) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;

  const bound = (ctx.data?.transcript?.roster || []).find((entry) => entry.key === event.key);
  if (bound) {
    event.preventDefault();
    assignSpeaker(ctx, bound.name);
    return;
  }

  switch (event.key) {
    case "j":
      event.preventDefault();
      ctx.setMode("reading");
      setCursor(ctx, ctx.cursorIndex + 1, { scroll: true });
      break;
    case "k":
      event.preventDefault();
      ctx.setMode("reading");
      setCursor(ctx, ctx.cursorIndex - 1, { scroll: true });
      break;
    case " ":
      event.preventDefault();
      togglePlay(ctx);
      break;
    case "ArrowLeft":
      event.preventDefault();
      nudge(ctx, -5);
      break;
    case "ArrowRight":
      event.preventDefault();
      nudge(ctx, 5);
      break;
    case "[":
    case "]": {
      event.preventDefault();
      const rate = stepRate(ctx, event.key === "]" ? 1 : -1);
      if (rate) ctx.notify(`Playing at ${rate}\u00d7`);
      break;
    }
    case "/":
      event.preventDefault();
      ctx.showTab("search");
      ctx.el.searchInput.focus();
      ctx.el.searchInput.select();
      break;
    case "e":
      event.preventDefault();
      enterEdit(ctx, ctx.cursorIndex);
      break;
    case "s": {
      event.preventDefault();
      // Break the block at the cursor into its captions, so a back-and-forth
      // Zoom filed as one turn can be labelled line by line.
      const chunk = ctx.chunks[ctx.cursorIndex];
      if (!chunk || chunk.cue_ids.length < 2) {
        ctx.notify("That block is already a single line.");
        break;
      }
      const first = chunk.cue_ids[0];
      chunk.cue_ids.forEach((id) => ctx.splitCues.add(id));
      regroup(ctx);
      renderTranscript(ctx);
      applyHighlights(ctx);
      setCursor(ctx, ctx.chunks.findIndex((c) => c.cue_ids[0] === first), { scroll: true });
      break;
    }
    case "h":
      event.preventDefault();
      save(ctx, {});
      break;
    case "c":
      event.preventDefault();
      copySelection(ctx);
      break;
    case "f":
      event.preventDefault();
      ctx.setMode(ctx.mode === "following" ? "reading" : "following");
      if (ctx.mode === "following") setCursor(ctx, chunkIndexAtTime(ctx, ctx.currentTime), { scroll: true });
      break;
    case "Escape":
      hideQuoteBar(ctx);
      window.getSelection()?.removeAllRanges();
      if (ctx.activeHighlightId) {
        ctx.activeHighlightId = null;
        applyHighlights(ctx);
      }
      break;
    default:
      break;
  }
});

/* ----------------------------------------------------------------- chrome -- */

ctx.el.tabSearch.addEventListener("click", () => ctx.showTab("search"));
ctx.el.tabHighlights.addEventListener("click", () => ctx.showTab("highlights"));

const THEME_KEY = "subtitle-search:theme";
const applyTheme = (theme) => {
  document.documentElement.dataset.theme = theme;
  remember(THEME_KEY, theme);
};
applyTheme(recall(THEME_KEY, "auto"));

ctx.el.themeToggle.addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  const current = document.documentElement.dataset.theme || "auto";
  applyTheme(order[(order.indexOf(current) + 1) % order.length]);
});

load().catch((error) => {
  ctx.notify(`Could not load the recording: ${error.message}`, { kind: "warn", key: null });
});

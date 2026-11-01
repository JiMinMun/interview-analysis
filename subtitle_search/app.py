"""FastAPI application.

Routes are namespaced by recording id from the start, even though single-folder
mode registers exactly one. That keeps library mode an addition rather than a
migration.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .editing import (
    EditError,
    apply_cue_edit,
    apply_cue_merge,
    apply_cue_split,
    apply_roster_edit,
    apply_selection_speaker,
    apply_speaker_edit,
)
from .alignment import AlignmentError
from .alignment import available as alignment_available
from .highlights import COLORS, HighlightError
from .library import (
    THEMES_FILENAME,
    ThemeStore,
    all_quotes,
    cooccurrence,
    delete_tag,
    metrics as canvas_metrics,
    packing_order,
    rename_tag,
    tag_index,
    untagged,
    vocabulary,
)
from .semantics import (
    EMBEDDINGS_FILENAME,
    Semantics,
    SemanticsUnavailable,
    VectorCache,
    neural_available,
    saturation,
)
from .media import serve_media
from .search import regex_search, search
from .session import Recording, RecordingRegistry
from .timings import coverage, unmeasured

STATIC_DIR = Path(__file__).parent / "static"

#: The HTML entry points. Held to the same revalidation rule as the scripts, so a
#: page and its modules can never come from two different versions of the tool.
PAGES = {"/", "/reader", "/quotes", "/themes"}

#: Tells "the client said nothing about where this card came from" apart from
#: "it came from the bare canvas", which is a real answer and arrives as null.
_MOVED_ABSENT = object()


#: Where the reloading server leaves the folder it was pointed at.
#:
#: Reload works by re-importing the app in a fresh process, which means the app
#: cannot be handed to uvicorn already built -- it has to be buildable from
#: nothing but an import string. So the one piece of runtime configuration
#: travels in the environment instead of as an argument.
FOLDER_ENV = "SUBTITLE_SEARCH_FOLDER"


def from_environment() -> FastAPI:
    """Build the app from the environment. The entry point uvicorn reloads.

    Every reload re-reads the folder from disk, so a transcript corrected outside
    the tool -- or a quotes file written by another copy of it -- is picked up.
    """
    folder = os.environ.get(FOLDER_ENV)
    if not folder:
        raise RuntimeError(
            f"{FOLDER_ENV} is not set; start the server with `subtitle-search <folder>`"
        )
    registry = RecordingRegistry()
    registry.add_library(Path(folder))
    return create_app(registry)


def create_app(registry: RecordingRegistry) -> FastAPI:
    app = FastAPI(title="subtitle-search", docs_url=None, redoc_url=None)
    app.state.registry = registry

    def require(recording_id: str) -> Recording:
        recording = registry.get(recording_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="unknown recording")
        return recording

    @app.get("/api/config")
    def get_config() -> dict:
        default = registry.default
        return {
            "recordings": [r.summary() for r in registry.list()],
            "default_recording_id": default.id if default else None,
            "colors": list(COLORS),
        }

    @app.get("/api/recordings/{recording_id}")
    def get_recording(recording_id: str) -> dict:
        return require(recording_id).payload()

    @app.get("/api/recordings/{recording_id}/parts/{part_index}/media")
    def get_part_media(recording_id: str, part_index: int, request: Request):
        recording = require(recording_id)
        path = recording.media_path(part_index)
        if path is None:
            raise HTTPException(status_code=404, detail="no media file for this part")
        return serve_media(path, request.headers.get("range"))

    @app.get("/api/recordings/{recording_id}/media")
    def get_media(recording_id: str, request: Request):
        """The first part's media. Kept so a single-recording folder has a plain URL."""
        return get_part_media(recording_id, 0, request)

    @app.get("/api/recordings/{recording_id}/search")
    def get_search(
        recording_id: str,
        q: str = Query("", description="query text"),
        mode: str = Query("fuzzy", pattern="^(fuzzy|regex)$"),
        limit: int = Query(60, ge=1, le=500),
    ) -> dict:
        recording = require(recording_id)
        if mode == "regex":
            try:
                results = regex_search(recording.transcript, q, limit=limit)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            results = search(recording.transcript, q, limit=limit)
        return {"query": q, "mode": mode, "results": results}

    @app.patch("/api/recordings/{recording_id}/cues/{cue_id}")
    def edit_cue(recording_id: str, cue_id: str, payload: dict = Body(...)) -> dict:
        """Correct one line of the transcript and save it to the source file."""
        recording = require(recording_id)
        try:
            return apply_cue_edit(recording, cue_id, payload.get("text", ""))
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc

    @app.put("/api/recordings/{recording_id}/roster")
    def edit_roster(recording_id: str, payload: dict = Body(...)) -> dict:
        """Set the speakers and their keys, without opening the transcript."""
        recording = require(recording_id)
        try:
            result = apply_roster_edit(recording, payload.get("speakers") or [])
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # Rostering changes what joins, so the reader takes the whole thing back.
        return {**result, "recording": recording.payload()}

    #: Captions measured per request. Small enough that the reader can show
    #: progress and stop partway, large enough that the model load amortizes.
    ALIGN_BATCH = 25

    @app.get("/api/recordings/{recording_id}/alignment")
    def alignment_state(recording_id: str) -> dict:
        """Whether word timings can be measured here, and how many exist."""
        recording = require(recording_id)
        ok, reason = alignment_available()
        return {
            "available": ok,
            "reason": reason,
            "coverage": coverage(recording.transcript),
        }

    @app.post("/api/recordings/{recording_id}/align")
    def align(recording_id: str, payload: dict = Body(default={})) -> dict:
        """Measure word timings for some captions, or for the next batch of them.

        Handed out in batches rather than run to completion in one request: the
        reader loops until nothing is left, which gives it progress to show and
        makes stopping halfway keep everything measured so far.
        """
        recording = require(recording_id)
        ok, reason = alignment_available()
        if not ok:
            raise HTTPException(status_code=503, detail=reason)

        requested = payload.get("cue_ids")
        if requested:
            targets = [c for c in (recording.transcript.cue(i) for i in requested) if c]
        else:
            limit = max(1, min(int(payload.get("limit") or ALIGN_BATCH), 200))
            targets = unmeasured(recording.transcript, limit)

        try:
            measured = recording.measure(targets)
        except AlignmentError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the timings: {exc}"
            ) from exc

        return {
            "measured": measured,
            "captions": len(targets),
            "coverage": coverage(recording.transcript),
            "remaining": len(unmeasured(recording.transcript, 10_000)),
        }

    @app.post("/api/recordings/{recording_id}/cues/{cue_id}/split")
    def split_cue(recording_id: str, cue_id: str, payload: dict = Body(...)) -> dict:
        """Cut one caption in two, so two speakers in one block can be separated."""
        recording = require(recording_id)
        try:
            result = apply_cue_split(
                recording,
                cue_id,
                payload.get("offset", 0),
                payload.get("text"),
                align=bool(payload.get("align", True)),
            )
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # A split renumbers every later cue, so the reader takes the whole thing back.
        return {**result, "recording": recording.payload()}

    @app.post("/api/recordings/{recording_id}/cues/{cue_id}/merge")
    def merge_cues(recording_id: str, cue_id: str, payload: dict = Body(...)) -> dict:
        """Join a run of captions into one -- undoing a split, or repairing Zoom's."""
        recording = require(recording_id)
        try:
            result = apply_cue_merge(
                recording,
                cue_id,
                payload.get("through") or cue_id,
                payload.get("expect"),
            )
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # A join renumbers every later cue, so the reader takes the whole thing back.
        return {**result, "recording": recording.payload()}

    @app.post("/api/recordings/{recording_id}/selection/speaker")
    def reattribute_selection(recording_id: str, payload: dict = Body(...)) -> dict:
        """Hand a selected passage to another speaker, cutting captions to fit it."""
        recording = require(recording_id)
        try:
            result = apply_selection_speaker(
                recording,
                payload.get("start_cue_id", ""),
                int(payload.get("start_char_offset") or 0),
                payload.get("end_cue_id", ""),
                int(payload.get("end_char_offset") or 0),
                payload.get("speaker", ""),
            )
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # Cutting and regrouping changes every block, so the reader takes it back.
        return {**result, "recording": recording.payload()}

    @app.patch("/api/recordings/{recording_id}/cues/{cue_id}/speaker")
    def edit_speaker(recording_id: str, cue_id: str, payload: dict = Body(...)) -> dict:
        """Reattribute a line, or a run of them, to a different speaker."""
        recording = require(recording_id)
        try:
            result = apply_speaker_edit(
                recording, cue_id, payload.get("speaker", ""), payload.get("through")
            )
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the transcript: {exc}"
            ) from exc
        # Regrouping changes every block, so the reader takes the whole thing back.
        return {**result, "recording": recording.payload()}

    @app.get("/api/recordings/{recording_id}/highlights")
    def list_highlights(recording_id: str) -> dict:
        recording = require(recording_id)
        return {
            "highlights": recording.store.list(),
            "known_tags": recording.store.known_tags(),
            "stale": recording.store.stale,
            "path": str(recording.store.path),
        }

    @app.post("/api/recordings/{recording_id}/highlights", status_code=201)
    def create_highlight(recording_id: str, payload: dict = Body(...)) -> dict:
        recording = require(recording_id)
        try:
            highlight = recording.store.create(payload)
        except HighlightError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"highlight": highlight, "known_tags": recording.store.known_tags()}

    @app.patch("/api/recordings/{recording_id}/highlights/{highlight_id}")
    def update_highlight(recording_id: str, highlight_id: str, payload: dict = Body(...)) -> dict:
        recording = require(recording_id)
        try:
            highlight = recording.store.update(highlight_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown highlight") from exc
        except HighlightError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"highlight": highlight, "known_tags": recording.store.known_tags()}

    @app.delete("/api/recordings/{recording_id}/highlights/{highlight_id}")
    def delete_highlight(recording_id: str, highlight_id: str) -> JSONResponse:
        recording = require(recording_id)
        if not recording.store.delete(highlight_id):
            raise HTTPException(status_code=404, detail="unknown highlight")
        return JSONResponse({"deleted": highlight_id})

    # -- the library, and the themes built across it ----------------------

    def themes() -> ThemeStore:
        return app.state.themes

    def corpus() -> list[dict]:
        return all_quotes(registry)

    @app.get("/api/library")
    def get_library() -> dict:
        quotes = corpus()
        return {
            "root": str(registry.root) if registry.root else None,
            "is_library": registry.is_library,
            "recordings": [r.summary() for r in registry.list()],
            "unreadable": [{"folder": name, "reason": why} for name, why in registry.failures],
            "quote_count": len(quotes),
            "untagged_count": len(untagged(quotes)),
            "tags": tag_index(quotes),
            "cooccurrence": cooccurrence(quotes, minimum=1),
            "colors": list(COLORS),
        }

    @app.get("/api/library/vocabulary")
    def get_vocabulary() -> dict:
        """The tag vocabulary of the whole study, for completing as you type."""
        return {"tags": vocabulary(registry)}

    # Tags travel in the body rather than the path: a code is free text and can
    # hold a slash.
    def _tag(payload: dict, key: str) -> str:
        tag = str(payload.get(key) or "").strip()
        if not tag:
            raise HTTPException(status_code=400, detail=f"{key} is required")
        return tag

    @app.post("/api/library/tags/rename")
    def rename_library_tag(payload: dict = Body(...)) -> dict:
        old, new = _tag(payload, "from"), _tag(payload, "to")
        return {"from": old, "to": new, **rename_tag(registry, old, new)}

    @app.post("/api/library/tags/delete")
    def delete_library_tag(payload: dict = Body(...)) -> dict:
        tag = _tag(payload, "tag")
        return {"tag": tag, **delete_tag(registry, tag)}

    @app.get("/api/library/quotes")
    def get_library_quotes() -> dict:
        return {"quotes": corpus()}

    @app.get("/api/library/search")
    def search_library(q: str = Query(""), limit: int = Query(40, ge=1, le=200)) -> dict:
        """Search every transcript at once, newest-scoped results first."""
        results = []
        for recording in registry.list():
            for hit in search(recording.transcript, q, limit=limit):
                results.append({**hit, "recording_id": recording.id, "recording_title": recording.title})
        results.sort(key=lambda r: (0 if r["kind"] == "exact" else 1, -r["score"]))
        return {"query": q, "results": results[:limit]}

    @app.get("/api/library/themes")
    def get_themes() -> dict:
        quotes = corpus()
        store = themes()
        store.prune({q["ref"] for q in quotes})
        return {**store.state(), "metrics": canvas_metrics()}

    @app.post("/api/library/themes", status_code=201)
    def create_theme(payload: dict = Body(default={})) -> dict:
        store = themes()
        theme = store.create(
            payload.get("title", ""),
            payload.get("color"),
            payload.get("box"),
            payload.get("note", ""),
            payload.get("cards"),
        )
        return {"theme": theme, **store.state()}

    @app.patch("/api/library/themes/{theme_id}")
    def update_theme(theme_id: str, payload: dict = Body(...)) -> dict:
        try:
            return {"theme": themes().update(theme_id, payload)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown theme") from exc

    @app.delete("/api/library/themes/{theme_id}")
    def delete_theme(theme_id: str) -> JSONResponse:
        if not themes().delete(theme_id):
            raise HTTPException(status_code=404, detail="unknown theme")
        return JSONResponse({"deleted": theme_id})

    @app.post("/api/library/themes/assign")
    def assign_quote(payload: dict = Body(...)) -> dict:
        ref = payload.get("ref")
        if not ref:
            raise HTTPException(status_code=400, detail="a quote reference is required")
        try:
            return {"themes": themes().assign(ref, payload.get("theme_id"), payload.get("index"))}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown theme") from exc

    @app.post("/api/library/themes/order")
    def reorder_themes(payload: dict = Body(...)) -> dict:
        return {"themes": themes().reorder(list(payload.get("order") or []))}

    # -- the canvas: the same themes, laid out on a plane -------------------
    #
    # Every one of these answers with the whole canvas rather than the piece it
    # touched. Dragging a card from one area to another changes two themes'
    # membership, and a client rebuilding that from a narrower reply is a client
    # that can drift from the file -- which, on a page whose whole content is
    # positions, would show up as quotes in the wrong places.

    def _ref(payload: dict) -> str:
        ref = payload.get("ref")
        if not ref:
            raise HTTPException(status_code=400, detail="a quote reference is required")
        return str(ref)

    def _theme_id(payload: dict, key: str = "theme_id") -> str | None:
        value = payload.get(key)
        return str(value) if value else None

    @app.post("/api/library/canvas/place")
    def place_card(payload: dict = Body(...)) -> dict:
        """Put a card down, in an area or loose on the canvas.

        ``moved_from`` present means a drag: the card it names is picked up
        rather than copied. Absent means a fresh card, which is how the same
        quote comes to sit in two themes at once.
        """
        moved = (
            _theme_id(payload, "moved_from") if "moved_from" in payload else _MOVED_ABSENT
        )
        try:
            return themes().place(
                _ref(payload),
                _theme_id(payload),
                payload.get("x"),
                payload.get("y"),
                **({} if moved is _MOVED_ABSENT else {"moved_from": moved}),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown theme") from exc

    @app.post("/api/library/canvas/unplace")
    def unplace_card(payload: dict = Body(...)) -> dict:
        """Take one card off the canvas, leaving other cards for the same quote."""
        return themes().unplace(_ref(payload), _theme_id(payload))

    @app.post("/api/library/canvas/positions")
    def move_cards(payload: dict = Body(...)) -> dict:
        moves = payload.get("moves")
        if not isinstance(moves, list):
            raise HTTPException(status_code=400, detail="moves must be a list")
        return themes().reposition([m for m in moves if isinstance(m, dict)])

    @app.post("/api/library/canvas/reshape")
    def reshape_area(payload: dict = Body(...)) -> dict:
        """Move or resize a theme's area. Its quotes travel with it."""
        theme_id = _theme_id(payload)
        if theme_id is None:
            raise HTTPException(status_code=400, detail="a theme is required")
        try:
            return {"theme": themes().reshape(theme_id, payload)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown theme") from exc

    @app.post("/api/library/canvas/tidy")
    def tidy_area(payload: dict = Body(...)) -> dict:
        """Pack one area's cards into a grid by speaker and time, and grow it to fit.

        The order lives with the quotes, not with the themes, so the corpus is
        read here and handed down -- the theme store knows references and boxes
        and has never needed to know who said anything.
        """
        theme_id = _theme_id(payload)
        if theme_id is None:
            raise HTTPException(status_code=400, detail="a theme is required")
        try:
            return themes().tidy(theme_id, packing_order(corpus()))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown theme") from exc

    # -- reading the corpus by meaning ------------------------------------

    def semantics_for(quotes: list[dict], neural: bool):
        """Vectors for the current corpus, rebuilt only when it changes.

        Encoding is the slow part, so the result is held against a fingerprint
        of the quote texts and the backend that produced it.
        """
        fingerprint = (
            tuple(sorted(f"{q['ref']}:{hash(q.get('text',''))}" for q in quotes)),
            bool(neural),
        )
        cached = getattr(app.state, "semantics", None)
        if cached and cached[0] == fingerprint:
            return cached[1]
        model = Semantics.build(quotes, app.state.vectors, prefer_neural=neural)
        app.state.semantics = (fingerprint, model)
        return model

    @app.get("/api/library/semantics")
    def get_semantics(
        neural: bool = Query(False, description="use the sentence-transformer model"),
        clusters: int = Query(0, ge=0, le=20),
    ) -> dict:
        quotes = corpus()
        try:
            model = semantics_for(quotes, neural)
            coords = model.project()
            labels, count = model.cluster(clusters or None)
            terms = model.cluster_terms(labels)
        except SemanticsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        grouped: dict[int, list[str]] = {}
        for ref, label in zip(model.refs, labels):
            grouped.setdefault(int(label), []).append(ref)

        return {
            "backend": model.backend,
            "projector": model.projector,
            "neural_available": neural_available(),
            "points": [
                {"ref": ref, "x": round(x, 5), "y": round(y, 5), "cluster": int(label)}
                for ref, (x, y), label in zip(model.refs, coords, labels)
            ],
            "clusters": [
                {
                    "id": label,
                    "size": len(refs),
                    "terms": terms.get(label, []),
                    "refs": refs,
                }
                for label, refs in sorted(grouped.items())
            ],
            "cluster_count": count,
            "loneliest": model.loneliest(),
            "saturation": saturation(quotes, [r.summary() for r in registry.list()]),
        }

    @app.get("/api/library/similar")
    def get_similar(
        ref: str = Query(...), k: int = Query(6, ge=1, le=40), neural: bool = Query(False)
    ) -> dict:
        try:
            model = semantics_for(corpus(), neural)
            return {"ref": ref, "similar": model.similar(ref, count=k)}
        except SemanticsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/library/suggestions")
    def get_suggestions(neural: bool = Query(False)) -> dict:
        """Where each unsorted quote would go, judged by the company it keeps."""
        quotes = corpus()
        store = themes()
        placed = {ref: theme["id"] for theme in store.list() for ref in theme["refs"]}
        if not placed:
            return {"suggestions": []}
        try:
            model = semantics_for(quotes, neural)
        except SemanticsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        out = []
        for quote in quotes:
            if quote["ref"] in placed:
                continue
            hint = model.suggest_theme(quote["ref"], placed)
            if hint:
                out.append({"ref": quote["ref"], **hint})
        out.sort(key=lambda s: -s["confidence"])
        return {"suggestions": out}

    @app.post("/api/library/themes/from-refs", status_code=201)
    def theme_from_refs(payload: dict = Body(...)) -> dict:
        """Make a theme out of a set of quotes, as drawn on the map."""
        refs = [str(ref) for ref in (payload.get("refs") or [])]
        if not refs:
            raise HTTPException(status_code=400, detail="no quotes were selected")
        store = themes()
        theme = store.create(payload.get("title", ""), payload.get("color"))
        for ref in refs:
            store.assign(ref, theme["id"])
        return {"theme": theme, **store.state()}

    # -- pages -------------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        # Always the library, whatever it holds. One entry point beats a home
        # page that changes shape depending on how many folders it found.
        return FileResponse(STATIC_DIR / "library.html")

    @app.get("/reader")
    def reader() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/quotes")
    def quotes_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "quotes.html")

    @app.get("/themes")
    def themes_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "themes.html")

    @app.middleware("http")
    async def revalidate_assets(request: Request, call_next):
        """Never let a page link a cached script against a newer one.

        The frontend is ES modules importing each other by name. A browser holding
        yesterday's ``util.js`` next to today's ``app.js`` does not degrade -- the
        import fails to link and the *entire* module graph dies, so the page loads
        and then does nothing at all, with an error that names no file. Asking for
        revalidation on every asset makes that state unreachable; with ETags
        already in place it costs one 304 per file.

        Media is left alone: those are large, immutable, and range-requested.
        """
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path in PAGES:
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    root = registry.root or Path.cwd()
    app.state.themes = ThemeStore(root / THEMES_FILENAME)
    app.state.vectors = VectorCache(root / EMBEDDINGS_FILENAME)
    app.state.semantics = None
    return app

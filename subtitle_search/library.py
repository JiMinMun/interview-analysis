"""Working across a whole set of recordings.

The reader is built around one session. Thematic analysis is not: it needs every
quote in the study at once, grouped by what they are about rather than by which
interview they came from. This module flattens the per-recording quote files
into one corpus, and stores the themes built on top of it.

Themes live in ``library.themes.json`` at the root of the library, beside the
recording folders rather than inside any of them, because a theme belongs to the
study and not to a participant. A theme holds references, never copies: the
quote text stays in the recording's own file, so correcting a transcript still
updates every theme that quote appears in.

The same themes are worked on two ways. The board is columns; the canvas is a
plane, where a theme is a resizable area and a quote is a card sitting somewhere
inside it. So the file records geometry as well as membership: an area's box, and
a position for every card. Membership is *derived* from the cards -- a card
inside an area is a quote in that theme -- because two records of one fact is two
records to get out of step. ``refs`` is kept in sync as the shape the board reads.

A card, not a quote, is the thing on the canvas. That distinction is what lets a
quote sit in two themes at once: two cards, one quote, the way you would
photocopy a post-it to pin it to two walls. Only one card per quote per area,
though, so a slip of the hand cannot quietly stack a quote on top of itself.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

THEMES_FILENAME = "library.themes.json"
SCHEMA_VERSION = 2

#: Where a quote that has not been sorted anywhere yet belongs.
UNSORTED = "unsorted"

# -- canvas geometry --------------------------------------------------------
#
# In canvas units, which are CSS pixels at zoom 1. These live here and not only
# in the stylesheet because the server does the *first* layout: a themes file
# written before the canvas existed records which quotes are in a theme but not
# where any of them sits, and those quotes have to land somewhere readable
# before the page is ever opened. Every later position comes from a drag.

CARD_W, CARD_H = 230.0, 150.0
CARD_GAP = 14.0
AREA_W, AREA_H = 520.0, 400.0
#: One column of cards, and enough height to see that an area is empty.
AREA_MIN_W, AREA_MIN_H = 272.0, 220.0
AREA_PAD = 14.0
#: Room at the top of an area for its title, note and buttons.
AREA_HEAD = 84.0
AREA_GAP = 56.0
AREAS_PER_ROW = 3

#: How far from the origin anything may be placed. Not a design limit -- a guard,
#: so a bad number from a client cannot strand an area where no amount of panning
#: will find it again.
CANVAS_LIMIT = 40000.0


def _finite(value, fallback: float = 0.0) -> float:
    """A usable float, whatever the client actually sent."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return number


def _coord(value, fallback: float = 0.0) -> float:
    return round(max(-CANVAS_LIMIT, min(CANVAS_LIMIT, _finite(value, fallback))), 2)


def _extent(value, minimum: float, fallback: float) -> float:
    return round(max(minimum, min(CANVAS_LIMIT, _finite(value, fallback))), 2)


def _has_box(theme: dict) -> bool:
    return all(isinstance(theme.get(key), (int, float)) for key in ("x", "y", "w", "h"))


def _overlaps(a: tuple, b: tuple) -> bool:
    return (
        a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
        and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
    )


def area_columns(width: float = AREA_W) -> int:
    """How many cards fit across an area of this width."""
    return max(1, int((width - 2 * AREA_PAD + CARD_GAP) // (CARD_W + CARD_GAP)))


def _slot(index: int, columns: int) -> tuple[float, float]:
    """The ``index``-th grid position inside an area, in area-local coordinates."""
    return (
        AREA_PAD + (index % columns) * (CARD_W + CARD_GAP),
        AREA_HEAD + (index // columns) * (CARD_H + CARD_GAP),
    )


def _inside(theme: dict, x: float, y: float) -> tuple[float, float]:
    """A position for a card in ``theme``, brought within its box.

    Held at every write rather than only checked on read, so "a card in an area
    is inside that area" is an invariant of the file and not a hope about the
    client. Nothing may sit over the header: the theme's own name is the one
    thing on the canvas that must always be readable.
    """
    right = max(AREA_PAD, theme["w"] - AREA_PAD - CARD_W)
    bottom = max(AREA_HEAD, theme["h"] - AREA_PAD - CARD_H)
    return (
        round(min(max(x, AREA_PAD), right), 2),
        round(min(max(y, AREA_HEAD), bottom), 2),
    )


def _fits(count: int, width: float = AREA_W) -> float:
    """The area height that shows ``count`` cards without scrolling."""
    rows = max(1, -(-max(1, count) // area_columns(width)))
    return max(AREA_H, AREA_HEAD + rows * (CARD_H + CARD_GAP) - CARD_GAP + AREA_PAD)


#: Distinguishes "this card came from nowhere -- add one" from "it came from the
#: bare canvas", which is a real origin and spells itself None.
_KEEP = object()


def packing_key(quote: dict) -> tuple:
    """Where a quote falls when an area is packed into a grid: speaker, then time.

    Who said it first, because a theme read down a column of one voice at a time
    is a theme you can argue with -- the same person's three remarks about trust
    sit together, and the place where somebody else takes over is visible. Then
    time, so each voice runs in the order it was said rather than in the order it
    happened to be dragged out.

    Quotes with nobody attributed sort last: they are the ones to fix, not the
    ones to read first. Recording is the final tiebreak, and does nothing except
    make the result the same every time it is computed.

    The canvas applies the same rule to grid view, in JavaScript. Two spellings
    of one sentence, which is the cheaper mistake: the alternative is asking the
    server to re-sort on every redraw of a view that writes nothing at all.
    """
    speaker = str(quote.get("speaker") or "").strip()
    return (
        0 if speaker else 1,
        speaker.lower(),
        float(quote.get("start_time") or 0.0),
        str(quote.get("recording_id") or ""),
    )


def packing_order(quotes: list[dict]) -> dict[str, tuple]:
    """``packing_key`` for every quote in the library, by reference."""
    return {quote["ref"]: packing_key(quote) for quote in quotes}


def metrics() -> dict:
    """The canvas geometry the page needs, from the one place it is decided.

    The server does the first layout, so it owns these numbers. Handing them to
    the client rather than restating them in the stylesheet is what keeps a card
    the same size in a box the server sized.
    """
    return {
        "card_w": CARD_W,
        "card_h": CARD_H,
        "card_gap": CARD_GAP,
        "area_pad": AREA_PAD,
        "area_head": AREA_HEAD,
        "area_min_w": AREA_MIN_W,
        "area_min_h": AREA_MIN_H,
        "area_w": AREA_W,
        "area_h": AREA_H,
        "limit": CANVAS_LIMIT,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def quote_ref(recording_id: str, highlight_id: str) -> str:
    return f"{recording_id}:{highlight_id}"


def all_quotes(registry) -> list[dict]:
    """Every quote in the library, tagged with where it came from."""
    quotes: list[dict] = []
    for recording in registry.list():
        for highlight in recording.store.list():
            quotes.append(
                {
                    **highlight,
                    "ref": quote_ref(recording.id, highlight["id"]),
                    "recording_id": recording.id,
                    "recording_title": recording.title,
                }
            )
    return quotes


def tag_index(quotes: list[dict]) -> list[dict]:
    """Per-tag counts, and how they spread across recordings.

    ``recording_count`` is the number that matters most in analysis: a tag on
    twenty quotes from one participant is that person's preoccupation, while the
    same tag across twelve participants is a finding.
    """
    index: dict[str, dict] = {}
    for quote in quotes:
        for tag in quote.get("tags") or []:
            entry = index.setdefault(
                tag, {"tag": tag, "quote_count": 0, "recordings": {}, "colors": {}}
            )
            entry["quote_count"] += 1
            rec = quote["recording_id"]
            entry["recordings"][rec] = entry["recordings"].get(rec, 0) + 1
            color = quote.get("color")
            if color:
                entry["colors"][color] = entry["colors"].get(color, 0) + 1

    summary = []
    for entry in index.values():
        entry["recording_count"] = len(entry["recordings"])
        entry["color"] = (
            max(entry["colors"].items(), key=lambda kv: kv[1])[0] if entry["colors"] else None
        )
        entry.pop("colors")
        summary.append(entry)
    return sorted(summary, key=lambda e: (-e["recording_count"], -e["quote_count"], e["tag"].lower()))


def vocabulary(registry) -> list[dict]:
    """Every tag ever used anywhere in the library, for suggesting completions.

    Wider than ``tag_index``, deliberately. That one reports tags with quotes
    behind them right now; this one also keeps tags whose last quote was
    untagged or deleted, because the reason to show a vocabulary while typing is
    to stop a fifth near-duplicate of a code you already invented.

    Each recording's quotes file already remembers its own tags permanently, so
    the union of those is durable without another file to keep in sync.
    """
    entries: dict[str, dict] = {}

    def entry(tag: str) -> dict:
        return entries.setdefault(
            tag, {"tag": tag, "quote_count": 0, "recording_count": 0, "recordings": []}
        )

    for recording in registry.list():
        # History first: tags typed here at any point, even if nothing carries
        # them now.
        for tag in recording.store.known_tags():
            entry(tag)

        used: set[str] = set()
        for highlight in recording.store.list():
            for tag in highlight.get("tags") or []:
                item = entry(tag)
                item["quote_count"] += 1
                used.add(tag)
        for tag in used:
            item = entries[tag]
            item["recording_count"] += 1
            item["recordings"].append(recording.id)

    # Most-established first: a tag on many recordings is the one to reuse.
    return sorted(
        entries.values(),
        key=lambda e: (-e["recording_count"], -e["quote_count"], e["tag"].lower()),
    )


def rename_tag(registry, old: str, new: str) -> dict:
    """Rename a code across the whole library.

    A code is a study-wide idea even though each recording stores its own tags,
    so renaming it in one interview and not the others would split it in two.
    """
    counts = [recording.store.rename_tag(old, new) for recording in registry.list()]
    return {"quote_count": sum(counts), "recording_count": sum(1 for n in counts if n)}


def delete_tag(registry, tag: str) -> dict:
    """Remove a code from every quote and every recording's history."""
    counts = [recording.store.delete_tag(tag) for recording in registry.list()]
    return {"quote_count": sum(counts), "recording_count": sum(1 for n in counts if n)}


def cooccurrence(quotes: list[dict], minimum: int = 1) -> list[dict]:
    """Tags that share a quote.

    Two codes that always travel together are usually one theme wearing two
    names, or a pair worth reading as cause and effect. Either way it is the
    cheapest signal that a codebook needs consolidating.
    """
    pairs: dict[tuple[str, str], dict] = {}
    for quote in quotes:
        tags = sorted({t for t in (quote.get("tags") or []) if t})
        for i, first in enumerate(tags):
            for second in tags[i + 1 :]:
                key = (first, second)
                entry = pairs.setdefault(
                    key, {"a": first, "b": second, "count": 0, "recordings": set()}
                )
                entry["count"] += 1
                entry["recordings"].add(quote["recording_id"])

    out = []
    for entry in pairs.values():
        if entry["count"] < minimum:
            continue
        entry["recording_count"] = len(entry["recordings"])
        entry.pop("recordings")
        out.append(entry)
    return sorted(out, key=lambda e: (-e["count"], e["a"].lower(), e["b"].lower()))


def untagged(quotes: list[dict]) -> list[dict]:
    return [q for q in quotes if not (q.get("tags") or [])]


# ------------------------------------------------------------------ themes --


@dataclass
class Theme:
    id: str
    title: str
    note: str
    color: str | None
    refs: list[str]


class ThemeStore:
    """Reads and writes the library's themes file.

    Same discipline as the quote store: every change is written immediately and
    atomically, and fields written by a later version survive a round trip.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data = self._load()
        if self._prepare_canvas():
            self._write()

    def _empty(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "updated_at": _now(),
            "themes": [],
            "cards": [],
        }

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("themes", [])
        if not isinstance(data["themes"], list):
            data["themes"] = []
        if not isinstance(data.get("cards"), list):
            data["cards"] = []
        for theme in data["themes"]:
            theme.setdefault("refs", [])
        return data

    def _write(self) -> None:
        self._data["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(self._data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise

    # -- first canvas layout ---------------------------------------------

    def _prepare_canvas(self) -> bool:
        """Give anything without coordinates somewhere to be.

        A themes file written before the canvas existed says which quotes are in
        a theme and nothing about where they sit. Opening the canvas empty and
        asking for that sorting again would be the worst possible answer, so the
        first read lays the themes out in a grid and packs each one's quotes
        inside it. Cheap, once, and everything afterwards is a drag.

        This also repairs a theme whose cards went missing -- from a hand edit,
        or a crash between two writes. Membership is what matters and ``refs``
        holds it, so a ref with no card gets a card rather than being dropped.
        """
        changed = False
        if self._data.get("version", 0) < SCHEMA_VERSION:
            # Never downgrade: a file from a later version is left saying so.
            self._data["version"] = SCHEMA_VERSION
            changed = True

        homeless = [theme for theme in self._data["themes"] if not _has_box(theme)]
        row_y, row_h = AREA_GAP, 0.0
        for position, theme in enumerate(homeless):
            column = position % AREAS_PER_ROW
            if column == 0 and position:
                row_y += row_h + AREA_GAP
                row_h = 0.0
            height = _fits(len(theme.get("refs") or []))
            theme.update(
                {
                    "x": AREA_GAP + column * (AREA_W + AREA_GAP),
                    "y": row_y,
                    "w": AREA_W,
                    "h": height,
                }
            )
            row_h = max(row_h, height)
            changed = True

        for theme in self._data["themes"]:
            if "collapsed" not in theme:
                theme["collapsed"] = False
                changed = True

        for theme in self._data["themes"]:
            columns = area_columns(theme["w"])
            for index, ref in enumerate(theme.get("refs") or []):
                if self._card(ref, theme["id"]) is not None:
                    continue
                x, y = _slot(index, columns)
                self._data["cards"].append(
                    {"ref": ref, "theme_id": theme["id"], "x": x, "y": y}
                )
                changed = True

        return self._contain() or changed

    def _contain(self) -> bool:
        """Pull every card inside the area that holds it.

        Dragging and resizing both clamp already, so this is for what they cannot
        reach: a hand-edited file, or a position written by a version of this tool
        that clamped differently. A card over its area's header hides the theme's
        own name, which is the one thing on the canvas that must always be
        readable -- so the file is corrected on the way in rather than drawn wrong.
        """
        moved = False
        boxes = {t["id"]: t for t in self._data["themes"] if _has_box(t)}
        for card in self._data["cards"]:
            theme = boxes.get(card.get("theme_id"))
            if theme is None:
                continue
            spot = _inside(theme, _finite(card.get("x")), _finite(card.get("y")))
            if spot != (card.get("x"), card.get("y")):
                card["x"], card["y"] = spot
                moved = True
        return moved

    # -- queries ---------------------------------------------------------

    def list(self) -> list[dict]:
        return self._data["themes"]

    def cards(self) -> list[dict]:
        return self._data["cards"]

    def state(self) -> dict:
        """Everything the canvas draws, in one payload.

        Every canvas edit returns this rather than just what it touched: moving a
        card between areas changes two themes' membership, and a client stitching
        that together from a narrower reply is a client that can drift.
        """
        return {
            "themes": self._data["themes"],
            "cards": self._data["cards"],
            "placed": sorted(self.placed_refs()),
            "on_canvas": sorted(self.on_canvas_refs()),
        }

    def _find(self, theme_id: str) -> dict | None:
        return next((t for t in self._data["themes"] if t.get("id") == theme_id), None)

    def _require(self, theme_id: str) -> dict:
        theme = self._find(theme_id)
        if theme is None:
            raise KeyError(theme_id)
        return theme

    def _card(self, ref: str, theme_id: str | None) -> dict | None:
        return next(
            (
                c
                for c in self._data["cards"]
                if c.get("ref") == ref and c.get("theme_id") == theme_id
            ),
            None,
        )

    def placed_refs(self) -> set[str]:
        """Quotes that are in a theme -- what the board calls sorted."""
        return {ref for theme in self._data["themes"] for ref in theme.get("refs", [])}

    def on_canvas_refs(self) -> set[str]:
        """Quotes with a card anywhere, including loose on the bare canvas.

        Wider than ``placed_refs``: the tray hides a quote once it is out on the
        canvas at all, because a quote you have already pulled out and parked
        next to an area is one you have dealt with, even though no theme claims
        it yet.
        """
        return {card["ref"] for card in self._data["cards"]}

    # -- mutations -------------------------------------------------------

    def create(
        self,
        title: str = "",
        color: str | None = None,
        box: dict | None = None,
        note: str = "",
        cards: list[dict] | None = None,
    ) -> dict:
        """Add a theme, somewhere the canvas can show it.

        ``box`` is where the canvas was clicked. Without one -- a theme made from
        the board, or from a lasso on the map -- it goes in the first grid spot
        that overlaps nothing, so a theme created away from the canvas is still
        findable on it rather than stacked under an existing area.

        ``cards`` puts quotes in it as it is made, at positions given. That exists
        so undoing a deleted area can put the area back exactly as it was in one
        call -- the arrangement inside an area is work, and an undo that restored
        the title but scrambled the contents would not be an undo.
        """
        width = _extent((box or {}).get("w"), AREA_MIN_W, AREA_W)
        height = _extent((box or {}).get("h"), AREA_MIN_H, AREA_H)
        if box and box.get("x") is not None and box.get("y") is not None:
            x, y = _coord(box["x"]), _coord(box["y"])
        else:
            x, y = self._vacancy(width, height)

        theme = {
            "id": uuid.uuid4().hex[:12],
            "title": title.strip() or "Untitled theme",
            "note": str(note or ""),
            "color": color,
            "refs": [],
            "x": x,
            "y": y,
            "w": width,
            "h": height,
            "collapsed": False,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._data["themes"].append(theme)

        for position, card in enumerate(cards or []):
            ref = str(card.get("ref") or "")
            if not ref or self._card(ref, theme["id"]) is not None:
                continue
            fallback = _slot(position, area_columns(width))
            self._data["cards"].append(
                {
                    "ref": ref,
                    "theme_id": theme["id"],
                    "x": _coord(card.get("x"), fallback[0]),
                    "y": _coord(card.get("y"), fallback[1]),
                }
            )
        if cards:
            self._sync_refs()
        self._write()
        return theme

    def _vacancy(self, width: float, height: float) -> tuple[float, float]:
        """A grid spot on the canvas where a new area lands on nothing."""
        boxes = [
            (t["x"], t["y"], t["w"], t["h"]) for t in self._data["themes"] if _has_box(t)
        ]
        for slot in range(2 * len(boxes) + AREAS_PER_ROW + 1):
            x = AREA_GAP + (slot % AREAS_PER_ROW) * (AREA_W + AREA_GAP)
            y = AREA_GAP + (slot // AREAS_PER_ROW) * (AREA_H + AREA_GAP)
            if not any(_overlaps((x, y, width, height), box) for box in boxes):
                return x, y
        return AREA_GAP, AREA_GAP

    def update(self, theme_id: str, patch: dict) -> dict:
        theme = self._require(theme_id)
        if "title" in patch:
            theme["title"] = str(patch["title"]).strip() or "Untitled theme"
        if "note" in patch:
            theme["note"] = str(patch["note"] or "")
        if "color" in patch:
            theme["color"] = patch["color"] or None
        if "collapsed" in patch:
            # Rolled up to its title, keeping its size and everything in it. A
            # study's themes are not all live at once, and a finished one taking
            # up a screenful of plane is a finished one in the way.
            theme["collapsed"] = bool(patch["collapsed"])
        theme["updated_at"] = _now()
        self._write()
        return theme

    def delete(self, theme_id: str) -> bool:
        theme = self._find(theme_id)
        if theme is None:
            return False
        self._data["themes"].remove(theme)
        # The cards go with it. They were positioned inside the area, so leaving
        # them behind would strand them at coordinates relative to a box that no
        # longer exists -- the quotes return to the tray instead.
        self._data["cards"] = [
            card for card in self._data["cards"] if card.get("theme_id") != theme_id
        ]
        self._write()
        return True

    def reorder(self, order: list[str]) -> list[dict]:
        position = {theme_id: index for index, theme_id in enumerate(order)}
        self._data["themes"].sort(key=lambda t: position.get(t["id"], len(position)))
        self._write()
        return self._data["themes"]

    def reshape(self, theme_id: str, box: dict) -> dict:
        """Move or resize an area.

        Card positions inside an area are relative to it, so moving one is a
        single number changing and every quote in it comes along -- which is the
        whole reason they are stored that way. Resizing has to answer for the
        cards a smaller box no longer covers, and does it by pulling them back
        inside rather than evicting them: a quote does not stop being part of a
        theme because the box around it was dragged in.
        """
        theme = self._require(theme_id)
        if "x" in box:
            theme["x"] = _coord(box["x"], theme["x"])
        if "y" in box:
            theme["y"] = _coord(box["y"], theme["y"])
        if "w" in box:
            theme["w"] = _extent(box["w"], AREA_MIN_W, theme["w"])
        if "h" in box:
            theme["h"] = _extent(box["h"], AREA_MIN_H, theme["h"])

        for card in self._data["cards"]:
            if card.get("theme_id") == theme_id:
                card["x"], card["y"] = _inside(theme, card["x"], card["y"])

        theme["updated_at"] = _now()
        self._write()
        return theme

    def place(
        self,
        ref: str,
        theme_id: str | None,
        x,
        y,
        moved_from: str | None | object = _KEEP,
    ) -> dict:
        """Put a card for ``ref`` at (x, y), in ``theme_id`` or loose on the canvas.

        Coordinates are relative to the area for a card in one, and absolute for
        a loose card.

        ``moved_from`` names the card this one came from, so dragging a card out
        of one area and into another moves it instead of leaving a copy behind.
        Left out, this *adds* a card -- which is how one quote comes to sit in two
        themes. There is only ever one card per quote per area, so placing into
        somewhere the quote already is just moves it.

        Callers that have no position to give -- the board, where a column has no
        coordinates to offer -- leave it out, and a card new to the area lands in
        the first free grid slot rather than on top of whatever is at the corner.
        A card already there keeps where it is.
        """
        theme = self._require(theme_id) if theme_id is not None else None
        if moved_from is not _KEEP and moved_from != theme_id:
            self._forget(ref, moved_from)

        card = self._card(ref, theme_id)
        if card is None:
            fallback = self._make_room(theme) if theme else (0.0, 0.0)
            card = {"ref": ref, "theme_id": theme_id, "x": fallback[0], "y": fallback[1]}
            self._data["cards"].append(card)
        card["x"], card["y"] = self._at(theme, _coord(x, card["x"]), _coord(y, card["y"]))

        self._sync_refs()
        self._write()
        return self.state()

    def unplace(self, ref: str, theme_id: str | None) -> dict:
        """Take one card off the canvas, leaving any others for the same quote."""
        self._forget(ref, theme_id)
        self._sync_refs()
        self._write()
        return self.state()

    @staticmethod
    def _at(theme: dict | None, x: float, y: float) -> tuple[float, float]:
        """Where a card may sit: inside its area, or anywhere at all when loose."""
        return _inside(theme, x, y) if theme else (x, y)

    def _forget(self, ref: str, theme_id: str | None) -> None:
        card = self._card(ref, theme_id)
        if card is not None:
            self._data["cards"].remove(card)

    def reposition(self, moves: list[dict]) -> dict:
        """Set several card positions at once.

        Arranging post-its is a lot of small movements. Sending them together
        means a drag that ends up touching four cards is one write to the file,
        not four -- and no window where the file records half of it.
        """
        for move in moves:
            theme_id = move.get("theme_id") or None
            card = self._card(str(move.get("ref") or ""), theme_id)
            if card is None:
                continue
            card["x"], card["y"] = self._at(
                self._find(theme_id) if theme_id else None,
                _coord(move.get("x"), card["x"]),
                _coord(move.get("y"), card["y"]),
            )
        self._write()
        return self.state()

    def tidy(self, theme_id: str, ranking: dict[str, tuple] | None = None) -> dict:
        """Pack an area's cards into a grid, and grow it to fit them.

        Free placement is the point of the canvas, and it is also how an area
        ends up with two cards on top of each other and a third off the bottom
        edge. This is the way back, per area, without undoing the sorting.

        ``ranking`` is ``packing_order``: speaker, then time. Tidying is the
        moment you have decided that where the cards happen to sit means
        nothing, so it puts them in an order that means something instead of
        preserving the arrangement you just gave up on. It is also the order grid
        view draws, which is what lets that view be a true preview of this.

        Without a ranking, position falls back to reading order -- an ordering
        that at least does not shuffle a tidy area on a second press.
        """
        theme = self._require(theme_id)
        mine = [c for c in self._data["cards"] if c.get("theme_id") == theme_id]
        if ranking:
            # A card whose quote has gone sorts last; prune will take it shortly.
            mine.sort(key=lambda c: ranking.get(c["ref"], (2, "", 0.0, "")))
        else:
            mine.sort(key=lambda c: (round(c["y"] / (CARD_H / 2)), c["x"]))
        theme["h"] = _fits(len(mine), theme["w"])
        columns = area_columns(theme["w"])
        for index, card in enumerate(mine):
            card["x"], card["y"] = _slot(index, columns)
        theme["updated_at"] = _now()
        self._sync_refs()
        self._write()
        return self.state()

    def assign(self, ref: str, theme_id: str | None, index: int | None = None) -> list[dict]:
        """Move a quote into one theme, or out of every theme.

        The board's move, and the map's: pick a theme and the quote leaves
        whichever others held it. The canvas is where a quote goes into two
        themes at once, because there you can see that it did -- a select box
        showing one theme could not say so, and would quietly undo the second.
        """
        for card in [c for c in self._data["cards"] if c.get("ref") == ref]:
            self._data["cards"].remove(card)

        if theme_id is not None:
            theme = self._require(theme_id)
            x, y = self._make_room(theme)
            self._data["cards"].append({"ref": ref, "theme_id": theme_id, "x": x, "y": y})
            theme["updated_at"] = _now()

        self._sync_refs()
        if theme_id is not None and index is not None:
            # The board can say where in its column the quote landed. Order is
            # the column's own, so it is applied after membership is derived.
            refs = self._require(theme_id)["refs"]
            refs.remove(ref)
            refs.insert(max(0, min(index, len(refs))), ref)
        self._write()
        return self._data["themes"]

    def _make_room(self, theme: dict) -> tuple[float, float]:
        """Clear a spot in an area for a card, growing the area if it has to.

        A quote can arrive in a theme without anyone saying where to put it --
        moved on the board, lassoed on the map. It still needs coordinates, and
        dropping it on top of a card already there would hide both of them.

        Overlap, not an exact match: cards sit where a hand left them, so a slot
        can be free of any card's *corner* and still be entirely underneath one.

        And the area gives way rather than the card. Every position is held
        inside the box that owns it, so squeezing a card back in would put it
        straight back on top of something -- an area out of room grows, which is
        what a person would do with the pen.
        """
        columns = area_columns(theme["w"])
        taken = [
            (c["x"], c["y"], CARD_W, CARD_H)
            for c in self._data["cards"]
            if c.get("theme_id") == theme["id"]
        ]
        spot = None
        for slot in range(len(taken) + 1):
            x, y = _slot(slot, columns)
            if not any(_overlaps((x, y, CARD_W, CARD_H), box) for box in taken):
                spot = (x, y)
                break
        if spot is None:
            # Every slot is under something: start a fresh row below the lot.
            spot = _slot(-(-(len(taken) + 1) // columns) * columns, columns)

        theme["h"] = round(max(theme["h"], spot[1] + CARD_H + AREA_PAD), 2)
        return spot

    def _sync_refs(self) -> None:
        """Make each theme's ref list say exactly what its cards say.

        Membership is a property of the cards: a card inside an area is a quote
        in that theme. ``refs`` is that same fact in the shape the board reads,
        so it is derived here rather than maintained alongside -- one fact, one
        place it is decided. Existing order is kept, because the board lets you
        order a column and re-deriving must not shuffle it.
        """
        for theme in self._data["themes"]:
            mine = {c["ref"] for c in self._data["cards"] if c.get("theme_id") == theme["id"]}
            ordered = [ref for ref in theme.get("refs") or [] if ref in mine]
            ordered += [
                c["ref"]
                for c in self._data["cards"]
                if c.get("theme_id") == theme["id"] and c["ref"] not in ordered
            ]
            theme["refs"] = ordered

    def prune(self, known: set[str]) -> int:
        """Drop references to quotes that no longer exist.

        A quote deleted in the reader would otherwise leave a hole in a theme
        that nothing accounts for, and a card on the canvas with nothing to draw.
        """
        cards = [card for card in self._data["cards"] if card.get("ref") in known]
        removed = len(self._data["cards"]) - len(cards)
        for theme in self._data["themes"]:
            keep = [ref for ref in theme["refs"] if ref in known]
            removed += len(theme["refs"]) - len(keep)
            theme["refs"] = keep
        if removed:
            self._data["cards"] = cards
            self._sync_refs()
            self._write()
        return removed

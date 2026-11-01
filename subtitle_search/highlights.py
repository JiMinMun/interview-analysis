"""Persistent highlight storage.

Highlights live in ``<vtt-basename>.highlights.json`` next to the recording, so
they travel with the folder rather than living in a database somewhere else.

Two properties matter here. Writes are atomic, because autosaving on every edit
means a crash would otherwise be able to truncate the file mid-write and take a
session's quotes with it. And unknown fields survive a round trip, so a file
written by a later version of this tool degrades rather than being silently
stripped on the next save.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Transcript

SCHEMA_VERSION = 1

#: Colors are named here and rendered by the frontend, so the stored file stays
#: readable and does not hard-code a hex value that a restyle would orphan.
COLORS = ("amber", "teal", "rose", "violet", "sage")
DEFAULT_COLOR = "amber"

#: Seek slightly before a quote so playback lands on its first word, not inside it.
SEEK_LEAD_IN = 0.75


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HighlightError(ValueError):
    """Raised for a highlight payload that cannot be anchored to the transcript."""


def resolve_span(transcript: Transcript, payload: dict) -> dict:
    """Resolve cue anchors into media times.

    The client sends where in the text the selection landed; times are computed
    here so the estimator has exactly one implementation, and so a stored
    highlight can be re-resolved later if that estimator improves.
    """
    start_cue = transcript.cue(payload.get("start_cue_id", ""))
    end_cue = transcript.cue(payload.get("end_cue_id", ""))
    if start_cue is None or end_cue is None:
        raise HighlightError("selection does not resolve to cues in this transcript")

    if start_cue.index > end_cue.index:
        start_cue, end_cue = end_cue, start_cue
        payload = {
            **payload,
            "start_cue_id": start_cue.id,
            "end_cue_id": end_cue.id,
            "start_char_offset": payload.get("end_char_offset", 0),
            "end_char_offset": payload.get("start_char_offset", 0),
        }

    start_offset = int(payload.get("start_char_offset") or 0)
    end_offset = int(payload.get("end_char_offset") or len(end_cue.text))

    start_time = start_cue.time_at_offset(start_offset)
    end_time = end_cue.time_at_offset(end_offset)
    if end_time < start_time:
        end_time = start_time

    return {
        "start_cue_id": start_cue.id,
        "end_cue_id": end_cue.id,
        "start_char_offset": start_offset,
        "end_char_offset": end_offset,
        "start_time": round(start_time, 3),
        "end_time": round(end_time, 3),
        "speaker": payload.get("speaker") or start_cue.speaker,
    }


def _normalize_tags(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for tag in raw:
        cleaned = str(tag).strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


class HighlightStore:
    """Reads and writes one recording's highlights file."""

    def __init__(self, path: Path, transcript: Transcript):
        self.path = path
        self.transcript = transcript
        #: Set when quotes were adopted from an older per-transcript file.
        self.migrated_from: str | None = None
        self._data = self._load()
        if self.migrated_from:
            self._write()

    # -- persistence ------------------------------------------------------

    def _empty(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "vtt_file": self.transcript.source_name,
            "vtt_sha256": self.transcript.sha256,
            "updated_at": _now(),
            "known_tags": [],
            "highlights": [],
        }

    def _adopt_legacy(self) -> dict | None:
        """Pick up quotes from an older per-transcript file, if one exists.

        Earlier versions named the file after the transcript. Once a folder is
        read as one session there is a single quotes file, so the old one is
        adopted rather than left behind. The original is never deleted or
        modified -- it stays on disk as a backup.
        """
        candidates = [
            path
            for path in sorted(self.path.parent.glob("*.highlights.json"))
            if path != self.path and path.is_file()
        ]
        for candidate in candidates:
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("highlights"):
                self.migrated_from = candidate.name
                data.setdefault("known_tags", [])
                return data
        return None

    def _load(self) -> dict:
        if not self.path.exists():
            return self._adopt_legacy() or self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Never destroy an unreadable file by overwriting it with an empty
            # one -- move it aside so the user still has whatever was in there.
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("highlights", [])
        data.setdefault("known_tags", [])
        if not isinstance(data["highlights"], list):
            data["highlights"] = []
        return data

    def _write(self) -> None:
        self._data["updated_at"] = _now()
        self._data["vtt_file"] = self.transcript.source_name
        self._data["vtt_sha256"] = self.transcript.sha256
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Temp file in the *same* directory, so os.replace is a true atomic
        # rename rather than a cross-device copy.
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

    # -- queries ----------------------------------------------------------

    @property
    def stale(self) -> bool:
        """True when the highlights were written against a different transcript."""
        stored = self._data.get("vtt_sha256")
        return bool(stored) and stored != self.transcript.sha256

    def list(self) -> list[dict]:
        return sorted(self._data["highlights"], key=lambda h: h.get("start_time", 0.0))

    def known_tags(self) -> list[str]:
        tags: list[str] = list(self._data.get("known_tags") or [])
        for highlight in self._data["highlights"]:
            for tag in highlight.get("tags") or []:
                if tag not in tags:
                    tags.append(tag)
        return sorted(tags, key=str.lower)

    def _find(self, highlight_id: str) -> dict | None:
        return next((h for h in self._data["highlights"] if h.get("id") == highlight_id), None)

    # -- mutations --------------------------------------------------------

    def create(self, payload: dict) -> dict:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HighlightError("cannot highlight an empty selection")

        span = resolve_span(self.transcript, payload)
        color = payload.get("color") or DEFAULT_COLOR
        if color not in COLORS:
            color = DEFAULT_COLOR

        timestamp = _now()
        highlight = {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "color": color,
            "note": str(payload.get("note") or ""),
            "tags": _normalize_tags(payload.get("tags")),
            "created_at": timestamp,
            "updated_at": timestamp,
            **span,
        }
        self._data["highlights"].append(highlight)
        self._merge_tags(highlight["tags"])
        self._write()
        return highlight

    def update(self, highlight_id: str, patch: dict) -> dict:
        highlight = self._find(highlight_id)
        if highlight is None:
            raise KeyError(highlight_id)

        # Merge in place so fields written by another version are carried through.
        if "note" in patch:
            highlight["note"] = str(patch["note"] or "")
        if "tags" in patch:
            highlight["tags"] = _normalize_tags(patch["tags"])
            self._merge_tags(highlight["tags"])
        if "color" in patch and patch["color"] in COLORS:
            highlight["color"] = patch["color"]
        if any(key in patch for key in ("start_cue_id", "end_cue_id")):
            highlight.update(resolve_span(self.transcript, {**highlight, **patch}))
        if "text" in patch and str(patch["text"]).strip():
            highlight["text"] = str(patch["text"]).strip()

        highlight["updated_at"] = _now()
        self._write()
        return highlight

    def delete(self, highlight_id: str) -> bool:
        highlight = self._find(highlight_id)
        if highlight is None:
            return False
        self._data["highlights"].remove(highlight)
        self._write()
        return True

    def remap_cue(self, cue_id: str, old_text: str, new_text: str) -> list[dict]:
        """Re-anchor quotes after the cue they sit in was corrected.

        Corrections happen while quotes are being saved, so an edit inside a
        quoted span has to move that quote's offsets rather than leave them
        pointing at characters that shifted underneath. The quote's own text is
        re-derived too, so a saved quote reflects the corrected transcript
        instead of preserving the error.
        """
        from .editing import remap_offset  # local import: editing imports models

        touched: list[dict] = []
        for highlight in self._data["highlights"]:
            changed = False
            if highlight.get("start_cue_id") == cue_id:
                highlight["start_char_offset"] = remap_offset(
                    old_text, new_text, int(highlight.get("start_char_offset") or 0)
                )
                changed = True
            if highlight.get("end_cue_id") == cue_id:
                highlight["end_char_offset"] = remap_offset(
                    old_text, new_text, int(highlight.get("end_char_offset") or 0)
                )
                changed = True
            if not changed:
                continue

            try:
                highlight.update(resolve_span(self.transcript, highlight))
            except HighlightError:
                continue
            refreshed = self.transcript.text_between(
                highlight["start_cue_id"],
                highlight["start_char_offset"],
                highlight["end_cue_id"],
                highlight["end_char_offset"],
            )
            if refreshed:
                highlight["text"] = refreshed
            highlight["updated_at"] = _now()
            touched.append(highlight)

        if touched:
            self._write()
        return touched

    def _remap(self, move) -> list[dict]:
        """Re-anchor every quote through ``move``, then re-resolve what changed.

        ``move`` takes ``(cue_id, offset)`` and returns where that anchor now
        lives. Splitting and merging both renumber the cues after them, and both
        move anchors that were inside the captions they touched -- only the
        arithmetic differs.
        """
        touched: list[dict] = []
        for highlight in self._data["highlights"]:
            start = move(
                highlight.get("start_cue_id", ""), int(highlight.get("start_char_offset") or 0)
            )
            end = move(
                highlight.get("end_cue_id", ""), int(highlight.get("end_char_offset") or 0)
            )
            if (start[0], start[1], end[0], end[1]) == (
                highlight.get("start_cue_id"),
                highlight.get("start_char_offset"),
                highlight.get("end_cue_id"),
                highlight.get("end_char_offset"),
            ):
                continue

            highlight["start_cue_id"], highlight["start_char_offset"] = start
            highlight["end_cue_id"], highlight["end_char_offset"] = end
            try:
                highlight.update(resolve_span(self.transcript, highlight))
            except HighlightError:
                continue
            refreshed = self.transcript.text_between(
                highlight["start_cue_id"], highlight["start_char_offset"],
                highlight["end_cue_id"], highlight["end_char_offset"],
            )
            if refreshed:
                highlight["text"] = refreshed
            highlight["updated_at"] = _now()
            touched.append(highlight)

        self._write()
        return touched

    def remap_split(
        self, split_index: int, split_offset: int, head_len: int, tail_lead: int
    ) -> list[dict]:
        """Re-anchor quotes after one cue became two.

        Splitting inserts a cue, and cue ids are positional, so every id after
        the split shifts by one -- without this, a quote saved earlier in the
        session would silently start pointing at its neighbour. Anchors inside
        the split cue land in whichever half now contains their words.
        """
        def move(cue_id: str, offset: int) -> tuple[str, int]:
            if not cue_id.startswith("c") or not cue_id[1:].isdigit():
                return cue_id, offset
            index = int(cue_id[1:])
            if index > split_index:
                return f"c{index + 1}", offset
            if index < split_index:
                return cue_id, offset
            if offset < split_offset:
                return cue_id, min(offset, head_len)
            return f"c{split_index + 1}", max(0, offset - split_offset - tail_lead)

        return self._remap(move)

    def remap_merge(
        self, first_index: int, starts: list[int], lengths: list[int]
    ) -> list[dict]:
        """Re-anchor quotes after a run of cues became one.

        The mirror of a split: ids after the run shift *down* by however many cues
        disappeared, and an anchor inside any of the absorbed captions moves to
        where that caption's words now sit inside the joined text.
        """
        count = len(starts)

        def move(cue_id: str, offset: int) -> tuple[str, int]:
            if not cue_id.startswith("c") or not cue_id[1:].isdigit():
                return cue_id, offset
            index = int(cue_id[1:])
            if index < first_index:
                return cue_id, offset
            if index >= first_index + count:
                return f"c{index - (count - 1)}", offset
            position = index - first_index
            return f"c{first_index}", starts[position] + min(max(offset, 0), lengths[position])

        return self._remap(move)

    def restate_speaker(self, cue_ids, speaker: str | None) -> list[dict]:
        """Say who said the quotes anchored in a run of reattributed captions.

        A quote stores its speaker, because that is what gets copied out and read
        in the sidebar months later. Reattributing the captions underneath it has
        to carry through, or the quote goes on crediting the wrong person -- and a
        misattributed quote is the one error in this tool that could end up in
        something published.
        """
        wanted = set(cue_ids)
        touched: list[dict] = []
        for highlight in self._data["highlights"]:
            if highlight.get("start_cue_id") not in wanted:
                continue
            if highlight.get("speaker") == speaker:
                continue
            highlight["speaker"] = speaker
            highlight["updated_at"] = _now()
            touched.append(highlight)
        if touched:
            self._write()
        return touched

    def rename_tag(self, old: str, new: str) -> int:
        """Call every use of ``old`` by ``new`` instead, and return how many quotes changed.

        Renaming onto a tag that already exists merges the two: a quote carrying
        both ends up with one. The history moves too, or the old name would keep
        being offered while typing and drift back into use.
        """
        new = new.strip()
        touched = 0
        for highlight in self._data["highlights"]:
            tags = highlight.get("tags") or []
            if old not in tags:
                continue
            highlight["tags"] = _normalize_tags([new if tag == old else tag for tag in tags])
            highlight["updated_at"] = _now()
            touched += 1

        known = self._data.setdefault("known_tags", [])
        remembered = old in known
        if remembered:
            self._data["known_tags"] = _normalize_tags([new if tag == old else tag for tag in known])
        if touched or remembered:
            self._write()
        return touched

    def delete_tag(self, tag: str) -> int:
        """Take ``tag`` off every quote and out of the history; return how many quotes lost it."""
        touched = 0
        for highlight in self._data["highlights"]:
            tags = highlight.get("tags") or []
            if tag not in tags:
                continue
            highlight["tags"] = [t for t in tags if t != tag]
            highlight["updated_at"] = _now()
            touched += 1

        known = self._data.setdefault("known_tags", [])
        remembered = tag in known
        if remembered:
            known.remove(tag)
        if touched or remembered:
            self._write()
        return touched

    def restamp(self) -> None:
        """Record the transcript's new digest after an edit, so it reads as current."""
        self._write()

    def _merge_tags(self, tags: list[str]) -> None:
        known = self._data.setdefault("known_tags", [])
        for tag in tags:
            if tag not in known:
                known.append(tag)

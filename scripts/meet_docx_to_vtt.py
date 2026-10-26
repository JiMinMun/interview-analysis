#!/usr/bin/env python3
"""Turn a Google Meet transcript (.docx) into a WebVTT file the reader can open.

Meet does not export captions. It exports a document: a heading or two, then
turns of ``Name: text`` under bare timestamp lines. Some documents stamp every
turn, to the second::

    00:00
    Interviewer: hi nice to meet you
    00:04
    Participant: hi

and older ones only every five minutes, with several turns under each mark::

    00:05:00
    Speaker 1: hi nice to meet you
    Speaker 2: hi

The stamp can also lead the turn on its own line -- ``[00:01] Name: hi``,
``00:01 Name: hi``, ``00:01 | Name: hi`` -- and the words can start on the line
after the name, as in ``00:01 Interviewer:`` over the answer.

A session recorded as several videos restarts its stamps at 00:00 for each one.
Every stamp earlier than the one before it is taken as such a restart, and that
chunk is laid one second after the last stamp before it, so the times run on
instead of repeating.

All of these read the same way: a mark starts a block, and the block runs to the next
mark. Turns sharing a block -- several under a five-minute mark, or one long turn
cut into captions -- split its time in proportion to how much was said. So
captions land exactly on every mark and roughly in between: a long pause inside
a block shifts everything after it. With a stamp on every turn that error is
small; with five-minute marks a quote's timestamp can be off by tens of seconds,
and measuring word timings against those caption edges is not reliable.

Speakers are written as WebVTT voice tags rather than ``Name:`` prefixes, so the
reader takes the speakers decided here as given instead of guessing again.

The last block runs until "Meeting ended after ..." if Meet wrote one, then
``--end``, then the length of the recording sitting beside the document.

A ``Name:`` line straight after a timestamp is always a turn. Anywhere else a
prefix has to pass the reader's own rules, so a line in somebody's speech that
happens to open with ``Note:`` does not become a new speaker.

The document is left where it is and never copied: the anonymizer skips .docx
files and reports them, so the original names stay in ``raw/``.

Usage:
    python scripts/meet_docx_to_vtt.py raw/                 # every .docx under raw/
    python scripts/meet_docx_to_vtt.py raw/P01/transcript.docx
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

# Runnable straight from a checkout, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subtitle_search.mediainfo import container_duration  # noqa: E402
from subtitle_search.vtt import (  # noqa: E402
    _accept_speakers,
    is_plausible_speaker_prefix,
    parse_timestamp,
    vtt_timestamp,
)

MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mov", ".m4a"}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_STAMP = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
#: A timestamp on a line of its own, bracketed or not: "00:01", "[00:01]".
_MARK_RE = re.compile(rf"^[\[(]?(?P<stamp>{_STAMP})[\])]?$")
#: A timestamp leading a turn: "[00:01] Name: ...", "00:01 | Name: ...".
_INLINE_RE = re.compile(rf"^[\[(]?(?P<stamp>{_STAMP})[\])]?\s*[|\-–—]?\s*(?P<rest>\S.*)$")
_ENDED_RE = re.compile(rf"meeting ended after\s+({_STAMP})", re.IGNORECASE)
#: The text may be empty when it starts on the next line: "00:01 Interviewer:".
_PREFIX_RE = re.compile(r"^(?P<name>[^:\n]{1,100}):(?:[ \t]+(?P<text>\S.*))?$")
_SENTENCE_RE = re.compile(r"(?<=[.?!])\s+")

#: Long turns are cut into captions so a quote can start somewhere other than
#: the top of a two-minute answer. Cut on sentences, then on words.
MAX_CAPTION_WORDS = 20

#: Where a restarted chunk begins: this long after the last stamp before it.
RESTART_GAP = 1.0

#: Used only when nothing says how long the last block ran.
WORDS_PER_SECOND = 2.5


class ConversionError(ValueError):
    """The document cannot be turned into a transcript."""


@dataclass
class Turn:
    speaker: str
    text: str


@dataclass
class Block:
    start: float
    turns: list[Turn] = field(default_factory=list)


@dataclass
class Result:
    vtt: str
    speakers: dict[str, int]
    captions: int
    blocks: int
    end: float
    end_source: str
    dropped: list[str]
    restarts: int = 0


# --------------------------------------------------------------- reading --


def docx_lines(path: Path) -> list[str]:
    """The document's text, one entry per line, in reading order.

    Only paragraphs in the body are read. A soft line break inside a paragraph
    is a line of its own, since Meet sometimes uses one between turns.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ConversionError(f"not a readable .docx: {exc}") from exc

    lines: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t":
                pieces.append(node.text or "")
            elif node.tag == f"{_W}tab":
                pieces.append(" ")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                pieces.append("\n")
        for line in "".join(pieces).split("\n"):
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)
    return lines


def split_stamps(lines: list[str]) -> list[str]:
    """Give a timestamp that leads a turn a line of its own.

    Then "[00:01] Name: hi", "00:01 | Name: hi" and "00:01" over "Name: hi" all
    read the same. Only a stamp followed by ``Name:`` is split off, so speech that
    happens to open with a time ("10:30 is when I get up") stays speech.
    """
    split: list[str] = []
    for line in lines:
        match = _INLINE_RE.match(line)
        if match and not _MARK_RE.match(line) and _PREFIX_RE.match(match.group("rest")):
            split.extend([match.group("stamp"), match.group("rest")])
        else:
            split.append(line)
    return split


def parse_lines(lines: list[str]) -> tuple[list[Block], float | None, list[str], int]:
    """Group lines into timed blocks of turns.

    Returns the blocks, the end Meet reported if it reported one, the lines that
    were left out, and how many times the stamps restarted. Anything before the first timestamp is the document's
    heading -- title, attendees -- unless it reads as a turn, in which case it
    starts the transcript at zero.
    """
    lines = split_stamps(lines)
    counts: dict[str, int] = {}
    # A turn straight after a timestamp is a speaker by position, whether or not
    # the name would pass on its own -- which is what lets someone who speaks once
    # through, in documents that stamp every turn.
    stamped: set[str] = set()
    after_mark = False
    for line in lines:
        match = _PREFIX_RE.match(line)
        if match:
            name = match.group("name").strip()
            counts[name] = counts.get(name, 0) + 1
            if after_mark and is_plausible_speaker_prefix(name):
                stamped.add(name)
        after_mark = bool(_MARK_RE.match(line))
    speakers = _accept_speakers(counts) | stamped

    blocks: list[Block] = []
    dropped: list[str] = []
    ended: float | None = None
    # A session recorded as several videos restarts its stamps at 00:00 for each
    # one. Each restart is laid one second after the last stamp before it, and
    # every stamp after it shifts by the same amount.
    offset = 0.0
    restarts = 0
    for line in lines:
        mark = _MARK_RE.match(line)
        if mark:
            start = parse_timestamp(mark.group("stamp")) + offset
            if blocks and start < blocks[-1].start:
                offset += blocks[-1].start + RESTART_GAP - start
                start = blocks[-1].start + RESTART_GAP
                restarts += 1
            # Stamps to the second repeat when turns are quick; those share the
            # second rather than each getting none of it.
            if not blocks or start > blocks[-1].start:
                blocks.append(Block(start))
            continue
        ended_match = _ENDED_RE.search(line)
        if ended_match:
            ended = parse_timestamp(ended_match.group(1))
            continue

        match = _PREFIX_RE.match(line)
        if match and match.group("name").strip() in speakers:
            if not blocks:
                blocks.append(Block(0.0))
            blocks[-1].turns.append(Turn(match.group("name").strip(), (match.group("text") or "").strip()))
        elif blocks and blocks[-1].turns:
            # A paragraph with no speaker carries on the turn above it.
            turn = blocks[-1].turns[-1]
            turn.text = f"{turn.text} {line}".strip()
        else:
            dropped.append(line)

    # A speaker line with nothing after it, ever, has nothing to time.
    for block in blocks:
        block.turns = [turn for turn in block.turns if turn.text]
    return blocks, ended, dropped, restarts


# --------------------------------------------------------------- timing --


def caption_texts(text: str) -> list[str]:
    """Cut one turn into captions of at most MAX_CAPTION_WORDS words."""
    captions: list[list[str]] = []
    current: list[str] = []
    for sentence in _SENTENCE_RE.split(text):
        words = sentence.split()
        if current and len(current) + len(words) > MAX_CAPTION_WORDS:
            captions.append(current)
            current = []
        # Only a sentence too long for any caption gets here with current empty.
        while len(words) > MAX_CAPTION_WORDS:
            captions.append(words[:MAX_CAPTION_WORDS])
            words = words[MAX_CAPTION_WORDS:]
        current.extend(words)
    captions.append(current)
    return [" ".join(words) for words in captions if words]


def recording_length(folder: Path) -> float | None:
    """The length of the one recording beside the document, if there is one."""
    durations = [
        container_duration(path)
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    ]
    durations = [d for d in durations if d]
    # Several lengths means an interrupted session; none of them is the end.
    if len(set(round(d) for d in durations)) == 1:
        return durations[0]
    return None


def convert(lines: list[str], end: float | None = None, recording: float | None = None) -> Result:
    blocks, ended, dropped, restarts = parse_lines(lines)
    blocks = [block for block in blocks if block.turns]
    if not blocks:
        raise ConversionError("no speaker turns found")

    last = blocks[-1]
    spoken = sum(len(turn.text.split()) for turn in last.turns)
    for value, source in (
        (ended, '"Meeting ended after" in the document'),
        (end, "--end"),
        (recording, "length of the recording"),
        (last.start + spoken / WORDS_PER_SECOND, f"guessed at {WORDS_PER_SECOND} words/s"),
    ):
        if value is not None and value > last.start:
            final, end_source = value, source
            break

    cues: list[str] = []
    speakers: dict[str, int] = {}
    for index, block in enumerate(blocks):
        stop = blocks[index + 1].start if index + 1 < len(blocks) else final
        captions = [
            (turn.speaker, text) for turn in block.turns for text in caption_texts(turn.text)
        ]
        total = sum(len(text) for _, text in captions)
        position = 0
        for speaker, text in captions:
            begin = block.start + (stop - block.start) * position / total
            position += len(text)
            finish = block.start + (stop - block.start) * position / total
            speakers[speaker] = speakers.get(speaker, 0) + 1
            cues.append(
                f"{len(cues) + 1}\n{vtt_timestamp(begin)} --> {vtt_timestamp(finish)}\n"
                f"<v {speaker}>{text}\n"
            )

    return Result(
        vtt="WEBVTT\n\n" + "\n".join(cues),
        speakers=speakers,
        captions=len(cues),
        blocks=len(blocks),
        end=final,
        end_source=end_source,
        dropped=dropped,
        restarts=restarts,
    )


# --------------------------------------------------------------- command --


def find_documents(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            # "~$name.docx" is Word's lock file for an open document.
            found.extend(p for p in sorted(path.rglob("*.docx")) if not p.name.startswith("~$"))
        else:
            found.append(path)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Google Meet transcripts (.docx) to WebVTT, written beside "
        "each document. Times between Meet's timestamps are estimated.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help=".docx files, or folders to search")
    parser.add_argument(
        "--end",
        metavar="HH:MM:SS",
        help="when the last block ends, if the document does not say and there is no recording",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would happen, write nothing")
    parser.add_argument("--force", action="store_true", help="overwrite an existing .vtt")
    args = parser.parse_args(argv)

    end = parse_timestamp(args.end) if args.end else None
    documents = find_documents(args.paths)
    if not documents:
        print("error: no .docx files found", file=sys.stderr)
        return 1

    failed = 0
    for document in documents:
        target = document.with_suffix(".vtt")
        print(f"\n{document}")
        try:
            if target.exists() and not args.force:
                raise ConversionError(f"{target.name} already exists (use --force)")
            result = convert(docx_lines(document), end, recording_length(document.parent))
        except (ConversionError, OSError) as exc:
            print(f"  skipped: {exc}")
            failed += 1
            continue

        print(f"  {result.captions} captions in {result.blocks} block(s), ends {vtt_timestamp(result.end)} ({result.end_source})")
        if result.restarts:
            print(f"  timestamps restarted {result.restarts} time(s); chunks laid end to end")
        for speaker, count in result.speakers.items():
            print(f"  {speaker}: {count}")
        for line in result.dropped:
            print(f"  left out: {line[:80]}")
        if not args.dry_run:
            target.write_text(result.vtt, encoding="utf-8")
            print(f"  wrote {target.name}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

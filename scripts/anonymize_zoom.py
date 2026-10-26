#!/usr/bin/env python3
"""Anonymize a directory of downloaded Zoom interview folders.

Each folder under the input path is one participant, and the folder's name is
that participant's ID. For every folder this writes a matching folder under the
output path: media copied verbatim, transcripts rewritten with speaker names
replaced.

Every decision is made from one folder's own transcripts. Nothing is inferred by
comparing folders to each other, so a folder anonymizes the same way whether it
is run alone or alongside fifty others.

Within a folder, speakers are labeled by the order they first speak, which makes
the result predictable rather than clever:

    first speaker   -> interviewer 1
    second speaker  -> the participant ID
    third onwards   -> interviewer 2, interviewer 3, ...

When that ordering is wrong -- the participant spoke first, or a colleague joined
before the participant did -- name the interviewers with ``--interviewer``. Those
are treated as interviewers wherever they fall, in every folder, and the
participant becomes the first remaining speaker.

The names to replace come only from speaker labels. Nothing else is treated as a
name, so places, employers, and products are left exactly as they are; this pass
is a first sweep over transcripts you are going to read anyway, not a substitute
for reading them.

Chat logs, transcript backups, and file types it does not recognize are skipped
rather than copied on the assumption they are harmless. Anything it could not
resolve is printed under REVIEW.

Usage:
    python scripts/anonymize_zoom.py INPUT_DIR OUTPUT_DIR --dry-run
    python scripts/anonymize_zoom.py INPUT_DIR OUTPUT_DIR
    python scripts/anonymize_zoom.py INPUT_DIR OUTPUT_DIR --interviewer "Ada Lovelace"
    python scripts/anonymize_zoom.py INPUT_DIR OUTPUT_DIR --only P03 P07 --force
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Runnable straight from a checkout, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subtitle_search.vtt import (  # noqa: E402
    VTTParseError,
    parse_cues,
    starts_with_function_word,
)

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
TRANSCRIPT_EXTENSIONS = {".vtt"}

#: Anything whose name suggests a chat log. Never copied.
CHAT_HINTS = ("chat",)

#: Pre-edit backups written by the transcript editor. They hold the text as it
#: arrived, so copying one would reintroduce every name the pass just removed.
BACKUP_SUFFIX = "_original"

#: Given names that are also ordinary words. Replacing these as standalone
#: tokens would corrupt normal sentences ("mark this down"), so they are left in
#: place and reported instead of guessed at.
AMBIGUOUS_TOKENS = {
    "april", "art", "august", "bill", "chase", "chuck", "dawn", "dick", "don",
    "drew", "earl", "faith", "frank", "gene", "grace", "guy", "hall", "holly",
    "hope", "hunter", "ivy", "jack", "joy", "june", "king", "lane", "lee",
    "mark", "max", "may", "mercy", "miles", "nick", "olive", "page", "pat",
    "pearl", "piper", "ray", "reed", "rich", "rob", "robin", "rose", "ruby",
    "sage", "scout", "sky", "sonny", "story", "summer", "tanner", "victor",
    "wade", "will",
}

#: Parentheticals holding these are pronoun tags, not nicknames.
PRONOUN_WORDS = {
    "he", "him", "his", "she", "her", "hers", "they", "them", "theirs",
    "ze", "zir", "xe", "xem", "ey", "em",
}

MIN_TOKEN_LENGTH = 3

_PARENTHETICAL_RE = re.compile(r"\(([^)]*)\)")
_NON_NAME_RE = re.compile(r"[^\w\s'-]", re.UNICODE)


#: What a lone speaker becomes. An in-person session recorded through one
#: microphone files the whole room under whoever started the meeting, so the
#: label names nobody in particular -- calling it the participant would be a
#: claim the transcript does not support.
UNKNOWN_LABEL = "unknown"


def interviewer_label(number: int) -> str:
    return f"interviewer {number}"


# --------------------------------------------------------------- name shapes --


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", _NON_NAME_RE.sub(" ", text)).strip().lower()


@dataclass(frozen=True)
class ParsedName:
    """A Zoom display name pulled apart into the bits worth replacing.

    Display names carry more than a name: a bracketed nickname, and often an
    affiliation after a comma, as in "B.F. (Jim) Lightning, Brown U. USA".
    Treating the whole string as one name would make "Brown" and "USA" into
    replacement tokens and rewrite those words wherever they appeared.
    """

    raw: str
    display: str
    nickname: str | None
    affiliation: str | None
    key: str
    tokens: tuple[str, ...]


def parse_name(raw: str) -> ParsedName:
    raw = raw.strip()

    nickname: str | None = None
    for inner in _PARENTHETICAL_RE.findall(raw):
        words = _normalize(inner).split()
        if not words or all(word in PRONOUN_WORDS for word in words) or "/" in inner:
            continue  # a pronoun tag, not a name
        nickname = inner.strip()
        break

    base = _PARENTHETICAL_RE.sub(" ", raw)
    affiliation: str | None = None
    display = base

    if "," in base:
        head, _, tail = base.partition(",")
        head, tail = head.strip(), tail.strip()
        # One word after the comma reads as "Lastname, Firstname"; more than one
        # reads as an affiliation ("Lightning, Brown U. USA").
        if head and tail and len(tail.split()) <= 1:
            display = f"{tail} {head}"
        elif head and tail:
            display, affiliation = head, tail

    display = re.sub(r"\s+", " ", display).strip(" ,;-")
    key = _normalize(display)

    tokens = [t for t in key.split(" ") if t]
    if nickname:
        tokens.extend(_normalize(nickname).split())

    return ParsedName(
        raw=raw,
        display=display,
        nickname=nickname,
        affiliation=affiliation,
        key=key,
        tokens=tuple(dict.fromkeys(tokens)),
    )


def literal_forms(name: ParsedName) -> list[str]:
    """Every literal spelling worth searching for, longest first.

    Includes the raw label with its affiliation, so a line attributing the whole
    string is replaced outright, while single-word patterns are drawn only from
    the name itself.
    """
    forms = {name.raw, name.display}
    forms.add(_PARENTHETICAL_RE.sub(" ", name.raw).strip(" ,;-"))
    words = name.display.split()
    if len(words) >= 2:
        forms.add(f"{words[-1]}, {words[0]}")
        if name.nickname:
            forms.add(f"{words[0]} {name.nickname} {' '.join(words[1:])}")
    return sorted({re.sub(r"\s+", " ", f).strip() for f in forms if f.strip()}, key=len, reverse=True)


def same_person(a: str, b: str) -> bool:
    """Whether two canonical names are one person joining under two labels.

    "Alex" and "Alex Chen" are the same participant rejoining on a phone.
    """
    first, second = set(a.split(" ")), set(b.split(" "))
    return bool(first and second and (first <= second or second <= first))


# ------------------------------------------------------------------ mapping --


@dataclass
class Mapping:
    """How this folder's names get replaced."""

    #: group key -> replacement label
    targets: dict[str, str] = field(default_factory=dict)
    #: group key -> the parsed labels seen for it
    names: dict[str, set[ParsedName]] = field(default_factory=dict)
    skipped_tokens: list[str] = field(default_factory=list)
    pattern: re.Pattern | None = None
    _replacements: dict[str, str] = field(default_factory=dict)

    def build(self) -> None:
        """Compile one regex covering every name, longest match winning.

        A single pass matters: replacing names one after another could let a
        later pattern match inside text an earlier one already produced.
        """
        branches: list[tuple[str, str, str, str]] = []

        for key, replacement in self.targets.items():
            parsed = self.names.get(key, set())
            for name in sorted(parsed, key=lambda n: len(n.raw), reverse=True):
                for form in literal_forms(name):
                    words = form.split()
                    if len(words) < 2:
                        continue  # single tokens handled below
                    # Joined on \s+ rather than escaped whole, so a transcript
                    # that wrapped or double-spaced a name still matches.
                    expression = r"\s+".join(re.escape(word) for word in words)
                    branches.append((f"(?i:{expression})", replacement, form[0], form[-1]))

            # A standalone first name, surname, or nickname -- never a word from
            # an affiliation, which is why parse_name keeps them apart. Matched
            # case-sensitively: a full name is unambiguous in any casing, a lone
            # token is not, and an unlisted word-like surname ("Fields",
            # "Church") would otherwise corrupt ordinary sentences.
            for token in sorted({t for name in parsed for t in name.tokens}):
                if len(token) < MIN_TOKEN_LENGTH:
                    continue
                if token in AMBIGUOUS_TOKENS:
                    self.skipped_tokens.append(token)
                    continue
                word = token.capitalize()
                branches.append((re.escape(word), replacement, word[0], word[-1]))

        # Longest first so "Alex Chen" beats "Alex".
        branches.sort(key=lambda b: len(b[0]), reverse=True)
        if not branches:
            self.pattern = None
            return

        parts = []
        for index, (expression, replacement, first, last) in enumerate(branches):
            group = f"n{index}"
            self._replacements[group] = replacement
            # \b only asserts a boundary next to a word character. A name ending
            # in punctuation -- "Alex Chen (she/her)" -- has none between ")" and
            # ":", so a blanket \b would stop the branch matching at all.
            left = r"\b" if first.isalnum() or first == "_" else ""
            right = r"\b" if last.isalnum() or last == "_" else ""
            parts.append(f"(?P<{group}>{left}{expression}{right})")
        self.pattern = re.compile("|".join(parts))

    def apply(self, text: str) -> tuple[str, int]:
        if not self.pattern:
            return text, 0
        count = 0

        def substitute(match: re.Match) -> str:
            nonlocal count
            count += 1
            return self._replacements[match.lastgroup]

        return self.pattern.sub(substitute, text), count

    def residual(self, text: str) -> list[str]:
        """Mapped names still present after replacement. Should always be empty.

        Matches equal to a label this pass just wrote are not leftovers: if a
        participant ID shares a word with a speaker's name, the replacement text
        matches the very pattern that produced it, and reporting that as a
        surviving name would be a false alarm.
        """
        if not self.pattern:
            return []
        written = set(self.targets.values())
        return sorted(
            {m.group(0) for m in self.pattern.finditer(text) if m.group(0) not in written}
        )


class FolderProblem(Exception):
    """The folder cannot be anonymized, so nothing is written."""


def group_speakers(speakers: list[ParsedName]) -> tuple[list[str], dict[str, set[ParsedName]]]:
    """Distinct people, in the order they first speak.

    Labels are merged first, so a participant who rejoins as "Alex" after being
    "Alex Chen" counts once rather than becoming a second speaker.
    """
    order: list[str] = []
    grouped: dict[str, set[ParsedName]] = {}
    for name in speakers:
        if not name.key:
            continue
        key = next((k for k in order if same_person(name.key, k)), None)
        if key is None:
            key = name.key
            order.append(key)
        grouped.setdefault(key, set()).add(name)
    return order, grouped


def build_mapping(
    speakers: list[ParsedName], participant_id: str, explicit: list[str]
) -> Mapping:
    """Assign a label to every speaker, by order of first appearance.

    The second speaker is the participant by default, because an interviewer
    almost always opens. ``--interviewer`` overrides that for named people, and
    the participant then falls to the first speaker not named -- which is why a
    folder where the participant spoke first is fixed by naming the interviewer
    rather than by any guessing here.
    """
    order, grouped = group_speakers(speakers)

    if not order:
        raise FolderProblem("no speakers found in the transcript")

    explicit_names = [parse_name(raw) for raw in explicit]
    explicit_keys = set()
    for named in explicit_names:
        key = next((k for k in order if same_person(named.key, k)), None)
        if key is None:
            # Named but never speaks here: still replaced if mentioned aloud.
            key = named.key
            order.append(key)
        grouped.setdefault(key, set()).add(named)
        explicit_keys.add(key)

    unnamed = [k for k in order if k not in explicit_keys]
    if not unnamed:
        raise FolderProblem(
            "every speaker was named as an interviewer, so there is no participant"
        )

    # One speaker and nothing said about who they are: the label is doing no
    # work, so it is replaced outright rather than promoted to the participant.
    if len(order) == 1 and not explicit_keys:
        mapping = Mapping()
        mapping.targets[order[0]] = UNKNOWN_LABEL
        mapping.names[order[0]] = grouped.get(order[0], set())
        mapping.build()
        return mapping
    # Position 2 onwards by default; falls back to the only speaker there is.
    participant_key = next((k for k in unnamed if order.index(k) >= 1), unnamed[0])

    mapping = Mapping()
    number = 0
    for key in order:
        if key == participant_key:
            mapping.targets[key] = participant_id
        else:
            number += 1
            mapping.targets[key] = interviewer_label(number)
        mapping.names[key] = grouped.get(key, set())

    mapping.build()
    return mapping


# ------------------------------------------------------------- file handling --


def is_chat_file(path: Path) -> bool:
    return any(hint in path.name.lower() for hint in CHAT_HINTS)


def is_backup_transcript(path: Path) -> bool:
    return path.suffix.lower() in TRANSCRIPT_EXTENSIONS and path.stem.endswith(BACKUP_SUFFIX)


def same_file(source: Path, target: Path) -> bool:
    """Whether ``target`` is already a copy of ``source``, as copy2 leaves one."""
    if not target.is_file():
        return False
    a, b = source.stat(), target.stat()
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def read_transcript(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def transcripts_in(folder: Path) -> list[Path]:
    return [
        p
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.suffix.lower() in TRANSCRIPT_EXTENSIONS and not is_backup_transcript(p)
    ]


def speakers_in(content: str) -> list[str]:
    """Speaker labels in first-appearance order, via the viewer's own detection.

    Reusing the parser matters: it decides speakers across the whole file rather
    than per line, so a sentence like "here's my point: I disagreed" is not
    mistaken for a speaker and relabeled.
    """
    try:
        cues, _ = parse_cues(content)
    except VTTParseError:
        return []
    seen: list[str] = []
    for cue in cues:
        if cue.speaker and cue.speaker not in seen:
            seen.append(cue.speaker)
    return seen


_LABEL_RE = re.compile(r"^(?P<prefix>[^:\n]{1,160}):[ \t]+\S")


def looks_like_a_missed_speaker(prefix: str) -> bool:
    """Deliberately looser than the detector's own test.

    Reusing ``is_plausible_speaker_prefix`` here would make this blind to exactly
    the case worth catching: a label the detector rejected was never replaced,
    and if the reviewer rejects it for the same reason nothing says so -- the two
    people simply merge into one and the run looks clean. So this reports
    anything that reads like an attribution and lets a person judge it.
    """
    words = prefix.split()
    if not words or len(words) > 16:
        return False
    if not prefix[:1].isalpha():
        return False
    return not starts_with_function_word(prefix)


def unmapped_labels(content: str, allowed: set[str]) -> dict[str, int]:
    """Speaker-looking labels left in an anonymized transcript, and their lines.

    Speaker detection is conservative: a label it is not confident about is never
    detected, so it is never replaced. Reported rather than fixed, since deciding
    whether "Correction:" is a person is a judgement call for a reader.
    """
    found: dict[str, int] = {}
    for number, line in enumerate(content.splitlines(), start=1):
        match = _LABEL_RE.match(line.strip())
        if not match:
            continue
        prefix = match.group("prefix").strip()
        if prefix in allowed or not looks_like_a_missed_speaker(prefix):
            continue
        found.setdefault(prefix, number)
    return found


@dataclass
class Result:
    participant_id: str
    copied: list[str] = field(default_factory=list)
    anonymized: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: Media already in the output, unchanged, so not copied again.
    kept: list[str] = field(default_factory=list)
    #: Transcripts in the output that this run does not write, removed by --force.
    removed: list[str] = field(default_factory=list)
    mapping_summary: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)
    replacements: int = 0
    #: Something went wrong; the run reports a failure.
    problem: str | None = None
    #: Left out on purpose, by a flag. Not a failure.
    excluded: str | None = None


def process_folder(
    folder: Path,
    destination: Path,
    participant_id: str,
    explicit: list[str],
    dry_run: bool,
    force: bool,
    skip_single_speaker: bool = False,
) -> Result:
    result = Result(participant_id=participant_id)

    files = sorted(p for p in folder.rglob("*") if p.is_file())
    if not transcripts_in(folder):
        result.problem = "no transcript (.vtt) found"
        return result

    speakers: list[ParsedName] = []
    for path in transcripts_in(folder):
        for label in speakers_in(read_transcript(path)):
            speakers.append(parse_name(label))

    if skip_single_speaker:
        distinct, grouped = group_speakers(speakers)
        if len(distinct) == 1:
            # One voice is usually a recording that only captured one side, and
            # the ordering rule would call that person the participant, which is
            # as likely to be the interviewer talking alone.
            only = sorted({n.display for n in grouped[distinct[0]]})
            result.excluded = f"only one speaker in the transcript ({' / '.join(only)})"
            return result

    try:
        mapping = build_mapping(speakers, participant_id, explicit)
    except FolderProblem as exc:
        result.problem = str(exc)
        return result

    for key, replacement in mapping.targets.items():
        shown = " / ".join(sorted({n.display for n in mapping.names.get(key, set())}))
        result.mapping_summary.append(f"{shown} -> {replacement}")
    for token in sorted(set(mapping.skipped_tokens)):
        result.review.append(
            f'"{token.capitalize()}" is also an ordinary word, so it was left alone '
            "where it stands by itself; the full name was still replaced"
        )

    if destination.exists() and any(destination.iterdir()) and not force and not dry_run:
        result.problem = f"output folder already exists and is not empty: {destination}"
        return result

    allowed_labels = set(mapping.targets.values())

    # Everything is decided before anything is written, so a failure partway
    # through cannot leave a half-anonymized folder behind.
    planned: list[tuple[Path, Path, str | None]] = []

    for path in files:
        relative = path.relative_to(folder)
        suffix = path.suffix.lower()

        if is_chat_file(path):
            result.skipped.append((str(relative), "chat log"))
            continue
        if is_backup_transcript(path):
            result.skipped.append((str(relative), "pre-edit transcript backup"))
            continue

        target_name, _ = mapping.apply(relative.name)
        target = destination / relative.parent / target_name

        if suffix in MEDIA_EXTENSIONS:
            if same_file(path, target):
                # Re-running for an updated transcript should not mean copying
                # every recording again.
                result.kept.append(str(target.relative_to(destination)))
                continue
            planned.append((path, target, None))
            result.copied.append(f"{relative} -> {target.relative_to(destination)}")
        elif suffix in TRANSCRIPT_EXTENSIONS:
            cleaned, hits = mapping.apply(read_transcript(path))
            result.replacements += hits

            leftover = mapping.residual(cleaned)
            if leftover:
                # A mapped name surviving means a bug, not an ambiguity.
                result.problem = f"names survived replacement in {relative}: {', '.join(leftover)}"
                return result

            for prefix, line in sorted(unmapped_labels(cleaned, allowed_labels).items(), key=lambda kv: kv[1]):
                result.review.append(
                    f'{relative} line {line} still reads as a speaker line, "{prefix}: ...", '
                    f'but "{prefix}" was never detected as a speaker so it was left as written'
                )

            planned.append((path, target, cleaned))
            result.anonymized.append(f"{relative} -> {target.relative_to(destination)}")
        else:
            result.skipped.append((str(relative), f"unhandled file type ({suffix or 'no extension'})"))

    # A transcript left over from an earlier run would be read as another part of
    # the session -- a renamed transcript, or the reader's backup of the old text.
    # Quotes and word timings stay: the reader notices when the text under them
    # has changed, and they are work that cannot be regenerated.
    stale: list[Path] = []
    if force and destination.exists():
        written = {target for _, target, cleaned in planned if cleaned is not None}
        stale = [
            p
            for p in sorted(destination.rglob("*"))
            if p.is_file() and p.suffix.lower() in TRANSCRIPT_EXTENSIONS and p not in written
        ]
        result.removed = [str(p.relative_to(destination)) for p in stale]

    if dry_run:
        return result

    destination.mkdir(parents=True, exist_ok=True)
    for path in stale:
        path.unlink()
    for source_path, target, cleaned in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        if cleaned is None:
            shutil.copy2(source_path, target)
        else:
            with target.open("w", encoding="utf-8", newline="") as handle:
                handle.write(cleaned)

    return result


def report_folder(result: Result) -> None:
    print(f"\n{result.participant_id}")
    if result.excluded:
        print(f"  EXCLUDED — {result.excluded}")
        return
    if result.problem:
        print(f"  SKIPPED — {result.problem}")
        return

    for line in result.mapping_summary:
        print(f"  {line}")
    print(f"  {len(result.anonymized)} transcript(s), {result.replacements} name(s) replaced")
    for line in result.anonymized:
        print(f"    rewrote {line}")
    for line in result.copied:
        print(f"    copied  {line}")
    for line in result.kept:
        print(f"    kept    {line}  (already there, unchanged)")
    for line in result.removed:
        print(f"    removed {line}  (old transcript, not written this run)")
    for name, why in result.skipped:
        print(f"    skipped {name}  ({why})")


def report(results: list[Result], dry_run: bool) -> None:
    for result in results:
        report_folder(result)

    ok = [r for r in results if not r.problem and not r.excluded]
    excluded = [r for r in results if r.excluded]
    failed = [r for r in results if r.problem]
    flagged = [r for r in ok if r.review]

    print(f"\n{'would process' if dry_run else 'processed'}: {len(ok)} folder(s)")
    if excluded:
        print(f"excluded on purpose: {len(excluded)} folder(s)")
        for result in excluded:
            print(f"  {result.participant_id}: {result.excluded}")
    if failed:
        print(f"skipped: {len(failed)} folder(s) — nothing was written for these")
        for result in failed:
            print(f"  {result.participant_id}: {result.problem}")

    if flagged:
        print("\nREVIEW — left as written, decide these while reading:")
        for result in flagged:
            for line in result.review:
                print(f"  {result.participant_id}: {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Anonymize Zoom interview folders: one folder per participant, "
        "folder name is the participant ID. Within each folder the first speaker "
        "becomes 'interviewer 1', the second becomes the participant, and any others "
        "become 'interviewer 2' onwards.",
    )
    parser.add_argument("input", type=Path, help="directory containing one folder per participant")
    parser.add_argument("output", type=Path, help="directory to write anonymized folders into")
    parser.add_argument(
        "--interviewer",
        action="append",
        default=None,
        metavar="NAME",
        help="treat this speaker as an interviewer wherever they fall in the order, "
        "in every folder (repeatable). Use when the participant spoke first, or when "
        "someone joined before them.",
    )
    parser.add_argument(
        "--skip-single-speaker",
        action="store_true",
        help="leave out folders whose transcript has only one speaker, which usually "
        "means the recording only captured one side",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would happen, write nothing")
    parser.add_argument(
        "--force",
        action="store_true",
        help="write into output folders that already have files, replacing their "
        "transcripts (corrections made in the reader are lost) and removing any old "
        "transcript this run does not write. Quotes and word timings are kept.",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="ID",
        help="process only these participant folders, e.g. after their transcripts "
        "were updated. Pair with --force to replace existing output.",
    )
    args = parser.parse_args(argv)

    if not args.input.is_dir():
        print(f"error: not a directory: {args.input}", file=sys.stderr)
        return 1
    if args.output.resolve() == args.input.resolve():
        print("error: output must not be the same directory as input", file=sys.stderr)
        return 1
    if args.input.resolve() in args.output.resolve().parents:
        print("error: output must not be inside input", file=sys.stderr)
        return 1

    folders = sorted(p for p in args.input.iterdir() if p.is_dir())
    if not folders:
        print(f"error: no participant folders found in {args.input}", file=sys.stderr)
        return 1
    if args.only:
        # A mistyped ID would otherwise quietly process nothing for that person.
        unknown = sorted(set(args.only) - {p.name for p in folders})
        if unknown:
            print(f"error: no such participant folder: {', '.join(unknown)}", file=sys.stderr)
            return 1
        folders = [p for p in folders if p.name in args.only]

    explicit = list(args.interviewer or [])
    print(f"{len(folders)} participant folder(s) in {args.input}")
    if explicit:
        print(f"always an interviewer: {', '.join(explicit)}")
    if args.dry_run:
        print("dry run — nothing will be written")

    # Each folder is decided entirely from its own transcripts.
    results = [
        process_folder(
            folder,
            args.output / folder.name,
            folder.name,
            explicit,
            args.dry_run,
            args.force,
            args.skip_single_speaker,
        )
        for folder in folders
    ]

    if not args.dry_run:
        args.output.mkdir(parents=True, exist_ok=True)

    report(results, args.dry_run)
    return 1 if any(r.problem for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

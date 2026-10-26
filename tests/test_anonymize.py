"""The anonymization pass.

It walks a directory of participant folders, but every decision is made from one
folder's own transcripts. These concentrate on the places where a plausible
implementation would quietly do the wrong thing: an affiliation turning ordinary
words into replacement tokens, a backup file carrying the original names through,
a nickname slipping past.
"""

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "anonymize_zoom.py"
_spec = importlib.util.spec_from_file_location("anonymize_zoom", _SCRIPT)
anon = importlib.util.module_from_spec(_spec)
sys.modules["anonymize_zoom"] = anon
_spec.loader.exec_module(anon)


INTERVIEW = """WEBVTT

1
00:00:01.000 --> 00:00:06.000
Sireesh Gururaja: Thanks for joining. I'm Sireesh, and I'll ask the questions today.

2
00:00:07.000 --> 00:00:13.000
Alex Chen: Happy to be here. Gururaja is a hard name to spell, by the way.

3
00:00:14.000 --> 00:00:21.000
Sireesh Gururaja: That's right. Alex, tell me how you handle interviews in Pittsburgh.

4
00:00:22.000 --> 00:00:29.000
Alex Chen: Sure. Chen is my family name, in case you need the spelling.
"""


def write_media(path: Path, seconds: float = 5.0) -> None:
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", len(payload) + 8, kind) + payload

    mvhd = b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000)) + b"\x00" * 76
    path.write_bytes(box(b"ftyp", b"isom" + b"\x00" * 8) + box(b"moov", box(b"mvhd", mvhd)))


def make_folder(tmp_path: Path, transcript: str, name: str, filename: str = "session.vtt") -> Path:
    folder = tmp_path / "raw" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_text(transcript, encoding="utf-8")
    return folder


@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "raw"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def out(tmp_path):
    return tmp_path / "clean"


@pytest.fixture
def folder(tmp_path, source_root):
    source = make_folder(
        tmp_path, INTERVIEW, "P07", filename="GMT20240301-140000_Recording.transcript.vtt"
    )
    write_media(source / "GMT20240301-140000_Recording_1920x1080.mp4")
    write_media(source / "GMT20240301-140000_Recording.m4a")
    (source / "GMT20240301-140000_Recording_Chat.txt").write_text("14:02:11 Alex Chen: hello\n")
    return source


def run(source_root, output, *extra):
    return anon.main([str(source_root), str(output), *extra])


def read(output: Path, participant: str, name: str = "session.vtt") -> str:
    return (output / participant / name).read_text()


VTT_HEAD = "WEBVTT\n\n"


def cue(index: int, label: str, text: str) -> str:
    start, end = index * 5, index * 5 + 4
    return f"{index}\n00:00:{start:02d}.000 --> 00:00:{end:02d}.000\n{label}: {text}\n\n"


# -- who gets which label ----------------------------------------------


def test_first_speaker_is_interviewer_and_second_is_the_participant(folder, source_root, out):
    assert run(source_root, out) == 0
    text = read(out, "P07", "GMT20240301-140000_Recording.transcript.vtt")

    assert "interviewer 1: Thanks for joining." in text
    assert "P07: Happy to be here." in text
    assert "Sireesh Gururaja" not in text and "Alex Chen" not in text


def test_later_speakers_become_further_interviewers(tmp_path, source_root, out):
    make_folder(
        tmp_path,
        VTT_HEAD
        + cue(1, "Ada Lovelace", "Welcome.")
        + cue(2, "Rita Alvarez", "Thanks for having me.")
        + cue(3, "Daniel Kwon", "I'm here to take notes.")
        + cue(4, "Hana Sato", "And I'm observing."),
        "P42",
    )

    assert run(source_root, out) == 0
    text = read(out, "P42")
    assert "interviewer 1: Welcome." in text
    assert "P42: Thanks for having me." in text
    assert "interviewer 2: I'm here to take notes." in text
    assert "interviewer 3: And I'm observing." in text


def test_the_participant_id_is_the_folder_name(folder, source_root, out):
    run(source_root, out)
    assert "P07:" in read(out, "P07", "GMT20240301-140000_Recording.transcript.vtt")


def test_each_folder_is_labeled_from_its_own_transcript_only(tmp_path, source_root, out):
    """No cross-folder inference: a folder reads the same alone or in a batch."""
    make_folder(tmp_path, VTT_HEAD + cue(1, "Ada Lovelace", "One.") + cue(2, "Rita Alvarez", "Two."), "P01")
    make_folder(tmp_path, VTT_HEAD + cue(1, "Daniel Kwon", "One.") + cue(2, "Hana Sato", "Two."), "P02")

    assert run(source_root, out) == 0
    # Ada opens P01 and Daniel opens P02; each is that folder's interviewer 1.
    assert "interviewer 1: One." in read(out, "P01")
    assert "P01: Two." in read(out, "P01")
    assert "interviewer 1: One." in read(out, "P02")
    assert "P02: Two." in read(out, "P02")


def test_every_folder_in_the_directory_is_processed(tmp_path, source_root, out):
    for name, speaker in (("P01", "Rita Alvarez"), ("P02", "Hana Sato"), ("P03", "Daniel Kwon")):
        make_folder(tmp_path, VTT_HEAD + cue(1, "Ada Lovelace", "Hi.") + cue(2, speaker, "Hello."), name)

    assert run(source_root, out) == 0
    for name in ("P01", "P02", "P03"):
        assert f"{name}: Hello." in read(out, name)


def test_one_bad_folder_does_not_stop_the_others(tmp_path, source_root, out):
    make_folder(tmp_path, INTERVIEW, "P16")
    empty = source_root / "P17"
    empty.mkdir(parents=True)
    write_media(empty / "session.mp4")  # no transcript

    assert run(source_root, out) == 1
    assert (out / "P16" / "session.vtt").exists()
    assert not (out / "P17").exists()


# -- correcting the order with --interviewer ---------------------------


def test_naming_an_interviewer_who_spoke_second(tmp_path, source_root, out):
    """A colleague joined before the participant did."""
    make_folder(
        tmp_path,
        VTT_HEAD
        + cue(1, "Ada Lovelace", "Welcome.")
        + cue(2, "Daniel Kwon", "Notetaker here.")
        + cue(3, "Rita Alvarez", "And I'm the one being interviewed."),
        "P43",
    )

    assert run(source_root, out, "--interviewer", "Daniel Kwon") == 0
    text = read(out, "P43")
    assert "interviewer 1: Welcome." in text
    assert "interviewer 2: Notetaker here." in text
    assert "P43: And I'm the one being interviewed." in text


def test_naming_the_interviewer_when_the_participant_spoke_first(tmp_path, source_root, out):
    make_folder(
        tmp_path,
        VTT_HEAD + cue(1, "Rita Alvarez", "Sorry, am I early?") + cue(2, "Ada Lovelace", "Not at all."),
        "P44",
    )

    # The default order gets this backwards, which is what the flag is for.
    run(source_root, out)
    assert "interviewer 1: Sorry, am I early?" in read(out, "P44")

    second = out.parent / "clean2"
    assert run(source_root, second, "--interviewer", "Ada Lovelace") == 0
    text = read(second, "P44")
    assert "P44: Sorry, am I early?" in text
    assert "interviewer 1: Not at all." in text


def test_the_interviewer_flag_applies_to_every_folder(tmp_path, source_root, out):
    make_folder(tmp_path, VTT_HEAD + cue(1, "Rita Alvarez", "Hi.") + cue(2, "Ada Lovelace", "Hello."), "P01")
    make_folder(tmp_path, VTT_HEAD + cue(1, "Hana Sato", "Hi.") + cue(2, "Ada Lovelace", "Hello."), "P02")

    assert run(source_root, out, "--interviewer", "Ada Lovelace") == 0
    assert "P01: Hi." in read(out, "P01")
    assert "P02: Hi." in read(out, "P02")


def test_an_interviewer_named_but_never_speaking_is_still_replaced(tmp_path, source_root, out):
    make_folder(
        tmp_path,
        VTT_HEAD
        + cue(1, "Ada Lovelace", "Ada here. Charles Babbage sends his apologies.")
        + cue(2, "Rita Alvarez", "Understood."),
        "P45",
    )

    assert run(source_root, out, "--interviewer", "Charles Babbage") == 0
    text = read(out, "P45")
    assert "Babbage" not in text and "Charles" not in text


def test_every_speaker_named_as_interviewer_is_refused(tmp_path, source_root, out):
    make_folder(tmp_path, VTT_HEAD + cue(1, "Ada Lovelace", "One.") + cue(2, "Daniel Kwon", "Two."), "P46")

    assert run(source_root, out, "--interviewer", "Ada Lovelace", "--interviewer", "Daniel Kwon") == 1
    assert not (out / "P46").exists()


# -- the substitution itself -------------------------------------------


def test_names_spoken_in_the_body_are_replaced(folder, source_root, out):
    """Scrubbing only the attribution would leave the names in what people say."""
    run(source_root, out)
    text = read(out, "P07", "GMT20240301-140000_Recording.transcript.vtt")

    assert "I'm interviewer 1, and I'll ask" in text
    assert "interviewer 1 is a hard name to spell" in text  # bare surname
    assert "P07, tell me how you handle" in text
    assert "P07 is my family name" in text
    for leaked in ("Sireesh", "Gururaja", "Alex", "Chen"):
        assert leaked not in text


def test_unrelated_named_entities_are_left_alone(folder, source_root, out):
    """Only speaker labels drive replacement, so places must survive untouched."""
    run(source_root, out)
    assert "Pittsburgh" in read(out, "P07", "GMT20240301-140000_Recording.transcript.vtt")


def test_timings_and_structure_are_untouched(folder, source_root, out):
    run(source_root, out)
    text = read(out, "P07", "GMT20240301-140000_Recording.transcript.vtt")

    assert text.startswith("WEBVTT")
    assert "00:00:14.000 --> 00:00:21.000" in text
    assert text.count("-->") == INTERVIEW.count("-->")


# -- awkward display names ---------------------------------------------

AFFILIATED = (
    VTT_HEAD
    + cue(1, "Ada Lovelace", "Thanks for making the time.")
    + cue(2, "B.F. (Jim) Lightning, Brown U. USA", "Glad to help. Everyone calls me Jim.")
    + cue(3, "Ada Lovelace", "Lightning, how do the browns of the USA compare?")
)


def test_a_display_name_with_nickname_and_affiliation_is_recognized(tmp_path, source_root, out):
    make_folder(tmp_path, AFFILIATED, "P30")

    assert run(source_root, out) == 0
    text = read(out, "P30")
    assert "P30: Glad to help." in text
    assert "Lightning" not in text and "B.F." not in text


def test_the_nickname_inside_brackets_is_replaced_too(tmp_path, source_root, out):
    """Dropping it as if it were a pronoun tag would leave a real name in place."""
    make_folder(tmp_path, AFFILIATED, "P30")
    run(source_root, out)

    text = read(out, "P30")
    assert "Everyone calls me P30." in text
    assert "Jim" not in text


def test_an_affiliation_does_not_become_a_replacement_token(tmp_path, source_root, out):
    """Otherwise "Brown" and "USA" would be rewritten wherever they appeared."""
    make_folder(tmp_path, AFFILIATED, "P30")
    run(source_root, out)
    assert "how do the browns of the USA compare?" in read(out, "P30")


def test_pronoun_tags_are_not_treated_as_nicknames(tmp_path, source_root, out):
    make_folder(
        tmp_path,
        VTT_HEAD + cue(1, "Ada Lovelace", "Ready?") + cue(2, "Alex Chen (she/her)", "Ready. She agreed already."),
        "P31",
    )

    assert run(source_root, out) == 0
    text = read(out, "P31")
    assert "P31: Ready." in text
    assert "She agreed already." in text  # the pronoun is not a name


@pytest.mark.parametrize("label", ["Alex Chen (she/her)", "Chen, Alex", "alex chen", "Alex  Chen"])
def test_name_variants_resolve_to_one_participant(tmp_path, source_root, out, label):
    make_folder(
        tmp_path,
        VTT_HEAD + cue(1, "Ada Lovelace", "Go ahead.") + cue(2, label, "Thanks very much.") + cue(3, label, "One more."),
        "P13",
    )

    assert run(source_root, out) == 0
    text = read(out, "P13")
    assert "P13: Thanks very much." in text
    assert "Alex" not in text and "Chen" not in text and "alex" not in text


def test_a_participant_rejoining_is_one_person_not_a_new_speaker(tmp_path, source_root, out):
    """Otherwise the rejoin would be numbered as another interviewer."""
    make_folder(
        tmp_path,
        VTT_HEAD + cue(1, "Ada Lovelace", "Ready?") + cue(2, "Alex Chen", "Yes.") + cue(3, "Alex", "Rejoined on my phone."),
        "P14",
    )

    assert run(source_root, out) == 0
    text = read(out, "P14")
    assert "P14: Rejoined on my phone." in text
    assert "interviewer 2" not in text


# -- what does and does not get copied ---------------------------------


def test_media_is_copied_byte_for_byte(folder, source_root, out):
    run(source_root, out)
    original = folder / "GMT20240301-140000_Recording_1920x1080.mp4"
    copied = out / "P07" / "GMT20240301-140000_Recording_1920x1080.mp4"

    assert copied.read_bytes() == original.read_bytes()
    assert (out / "P07" / "GMT20240301-140000_Recording.m4a").exists()


def test_chat_files_are_never_copied(folder, source_root, out):
    run(source_root, out)
    assert not list((out / "P07").glob("*Chat*"))
    assert not list((out / "P07").glob("*.txt"))


def test_pre_edit_backups_are_never_copied(tmp_path, source_root, out):
    """A backup holds the transcript as it arrived, names and all."""
    source = make_folder(tmp_path, INTERVIEW, "P09")
    (source / "session_original.vtt").write_text(INTERVIEW, encoding="utf-8")
    write_media(source / "session.mp4")

    run(source_root, out)
    assert "session_original.vtt" not in [p.name for p in (out / "P09").iterdir()]
    for path in (out / "P09").rglob("*.vtt"):
        assert "Sireesh Gururaja" not in path.read_text()


def test_unrecognized_files_are_skipped_not_copied(tmp_path, source_root, out):
    source = make_folder(tmp_path, INTERVIEW, "P11")
    (source / "notes.docx").write_bytes(b"binary notes mentioning Alex Chen")
    (source / "session.highlights.json").write_text('{"highlights": []}')

    run(source_root, out)
    assert sorted(p.name for p in (out / "P11").iterdir()) == ["session.vtt"]


def test_names_in_filenames_are_rewritten(tmp_path, source_root, out):
    source = make_folder(tmp_path, INTERVIEW, "P12", filename="Alex Chen interview.vtt")
    write_media(source / "Alex Chen interview.mp4")

    run(source_root, out)
    assert sorted(p.name for p in (out / "P12").iterdir()) == [
        "P12 interview.mp4",
        "P12 interview.vtt",
    ]


def test_multiple_transcripts_share_one_mapping(tmp_path, source_root, out):
    """An interrupted session has several transcripts."""
    source = make_folder(tmp_path, INTERVIEW, "P21", filename="GMT20240301-140000_Recording.vtt")
    (source / "GMT20240301-141200_Recording.vtt").write_text(
        VTT_HEAD + cue(1, "Alex Chen", "I'm back after the drop."), encoding="utf-8"
    )

    assert run(source_root, out) == 0
    assert "P21: I'm back after the drop." in read(out, "P21", "GMT20240301-141200_Recording.vtt")


# -- what it reports rather than decides -------------------------------


def test_an_undetected_speaker_label_is_reported_not_refused(tmp_path, source_root, out, capsys):
    """Detection is conservative; a reader decides what the leftovers are."""
    make_folder(
        tmp_path,
        VTT_HEAD
        + cue(1, "Ada Lovelace", "Welcome.")
        + cue(2, "Alex Chen", "Glad to be here.")
        + cue(3, "morgan reyes", "Sorry, wrong meeting."),
        "P23",
    )

    assert run(source_root, out) == 0  # written, not blocked
    output = capsys.readouterr().out
    assert "REVIEW" in output
    assert "morgan reyes" in output
    assert "P23" in output
    assert (out / "P23" / "session.vtt").exists()


def test_word_like_first_names_are_left_alone_and_reported(tmp_path, source_root, out, capsys):
    """Replacing a name that is also a word would corrupt ordinary sentences."""
    make_folder(
        tmp_path,
        VTT_HEAD
        + cue(1, "Ada Lovelace", "Please mark that down as a summer project.")
        + cue(2, "Mark Summer", "Will do."),
        "P19",
    )

    run(source_root, out)
    text = read(out, "P19")
    output = capsys.readouterr().out

    assert "P19: Will do." in text
    assert "Please mark that down as a summer project." in text
    assert "REVIEW" in output and "Mark" in output and "Summer" in output


def test_a_colon_in_a_sentence_is_not_taken_for_a_speaker(tmp_path, source_root, out):
    """Reusing the viewer's parser means the colon trap is handled here too."""
    make_folder(
        tmp_path,
        VTT_HEAD
        + cue(1, "Ada Lovelace", "So here's my whole point: I disagreed with the framing.")
        + cue(2, "Alex Chen", "Understood."),
        "P22",
    )

    assert run(source_root, out) == 0
    assert "my whole point: I disagreed" in read(out, "P22")


# -- flags and guards --------------------------------------------------


def test_dry_run_writes_nothing(folder, source_root, out):
    assert run(source_root, out, "--dry-run") == 0
    assert not out.exists()


def test_existing_output_is_not_overwritten(folder, source_root, out):
    run(source_root, out)
    stamp = out / "P07" / "GMT20240301-140000_Recording.transcript.vtt"
    stamp.write_text("edited by hand")

    assert run(source_root, out) == 1
    assert stamp.read_text() == "edited by hand"
    assert run(source_root, out, "--force") == 0
    assert stamp.read_text() != "edited by hand"


def test_only_processes_the_named_folders(tmp_path, source_root, out):
    make_folder(tmp_path, INTERVIEW, "P01")
    make_folder(tmp_path, INTERVIEW, "P02")
    assert run(source_root, out, "--only", "P02") == 0
    assert not (out / "P01").exists()
    assert (out / "P02" / "session.vtt").exists()


def test_only_refuses_an_id_with_no_folder(tmp_path, source_root, out):
    make_folder(tmp_path, INTERVIEW, "P01")
    assert run(source_root, out, "--only", "P10") == 1
    assert not out.exists()


def test_an_updated_transcript_replaces_the_old_one_and_leaves_the_rest(tmp_path, source_root, out):
    first = make_folder(tmp_path, INTERVIEW, "P01")
    make_folder(tmp_path, INTERVIEW, "P02")
    write_media(first / "session.mp4")
    run(source_root, out)

    other = out / "P02" / "session.vtt"
    other.write_text("corrected in the reader")
    (out / "P01" / "session_original.vtt").write_text("the reader's backup of the old text")
    (out / "P01" / "session.highlights.json").write_text("{}")

    # The update arrives under a new name, as a re-export often does.
    (first / "session.vtt").unlink()
    (first / "updated.vtt").write_text(INTERVIEW.replace("ask the questions", "ask a few questions"))

    assert run(source_root, out, "--only", "P01", "--force") == 0
    assert sorted(p.name for p in (out / "P01").iterdir()) == [
        "session.highlights.json",
        "session.mp4",
        "updated.vtt",
    ]
    assert "ask a few questions" in read(out, "P01", "updated.vtt")
    assert other.read_text() == "corrected in the reader"


def test_unchanged_media_is_not_copied_again(folder, source_root, out, capsys):
    run(source_root, out)
    video = out / "P07" / "GMT20240301-140000_Recording_1920x1080.mp4"
    before = video.stat().st_ino, video.stat().st_mtime_ns

    capsys.readouterr()
    assert run(source_root, out, "--force") == 0
    assert (video.stat().st_ino, video.stat().st_mtime_ns) == before
    assert "kept" in capsys.readouterr().out


def test_a_dry_run_reports_what_force_would_remove_and_removes_nothing(tmp_path, source_root, out, capsys):
    make_folder(tmp_path, INTERVIEW, "P01")
    run(source_root, out)
    old = out / "P01" / "old.vtt"
    old.write_text(INTERVIEW)

    capsys.readouterr()
    assert run(source_root, out, "--force", "--dry-run") == 0
    assert "removed old.vtt" in capsys.readouterr().out
    assert old.exists()


def test_output_inside_input_is_refused(folder, source_root):
    assert anon.main([str(source_root), str(source_root / "nested")]) == 1
    assert anon.main([str(source_root), str(source_root)]) == 1


def test_a_folder_without_a_transcript_is_refused(tmp_path, source_root, out):
    empty = source_root / "P18"
    empty.mkdir(parents=True)
    write_media(empty / "session.mp4")

    assert run(source_root, out) == 1
    assert not (out / "P18").exists()


def test_an_empty_input_directory_is_an_error(source_root, out):
    assert run(source_root, out) == 1


def test_a_participant_id_that_collides_with_a_name_is_not_a_false_alarm(tmp_path, source_root, out):
    """The replacement text can match the pattern that produced it."""
    make_folder(tmp_path, VTT_HEAD + cue(1, "Ada Lovelace", "Hi.") + cue(2, "Robin P07 Fields", "Hello."), "P07")

    assert run(source_root, out) == 0
    assert "P07: Hello." in read(out, "P07")


# -- excluding single-speaker folders ----------------------------------


def test_a_single_speaker_folder_is_processed_by_default(tmp_path, source_root, out):
    make_folder(tmp_path, VTT_HEAD + cue(1, "Ada Lovelace", "Testing, one two.") + cue(2, "Ada Lovelace", "Still me."), "P50")

    assert run(source_root, out) == 0
    assert (out / "P50" / "session.vtt").exists()


def test_skip_single_speaker_leaves_that_folder_out(tmp_path, source_root, out, capsys):
    make_folder(tmp_path, VTT_HEAD + cue(1, "Ada Lovelace", "Testing, one two.") + cue(2, "Ada Lovelace", "Still me."), "P50")

    assert run(source_root, out, "--skip-single-speaker") == 0
    assert not (out / "P50").exists()
    output = capsys.readouterr().out
    assert "EXCLUDED" in output
    assert "only one speaker" in output


def test_excluding_a_folder_is_not_a_failure(tmp_path, source_root, out):
    """Leaving it out was asked for, so the run still reports success."""
    make_folder(tmp_path, VTT_HEAD + cue(1, "Ada Lovelace", "Alone."), "P50")
    make_folder(tmp_path, VTT_HEAD + cue(1, "Ada Lovelace", "Hi.") + cue(2, "Rita Alvarez", "Hello."), "P51")

    assert run(source_root, out, "--skip-single-speaker") == 0
    assert not (out / "P50").exists()
    assert (out / "P51" / "session.vtt").exists()


def test_a_rejoining_speaker_still_counts_as_one(tmp_path, source_root, out):
    """"Alex" after "Alex Chen" is one voice, so the folder is excluded."""
    make_folder(tmp_path, VTT_HEAD + cue(1, "Alex Chen", "Only me here.") + cue(2, "Alex", "Rejoined."), "P52")

    assert run(source_root, out, "--skip-single-speaker") == 0
    assert not (out / "P52").exists()


def test_two_speakers_are_unaffected_by_the_flag(folder, source_root, out):
    assert run(source_root, out, "--skip-single-speaker") == 0
    assert (out / "P07" / "GMT20240301-140000_Recording.transcript.vtt").exists()


def test_a_single_speaker_folder_is_counted_across_its_transcripts(tmp_path, source_root, out):
    """An interrupted session is one conversation, so count over all its parts."""
    source = make_folder(tmp_path, VTT_HEAD + cue(1, "Ada Lovelace", "Part one, alone."), "P53",
                         filename="GMT20240301-140000_Recording.vtt")
    (source / "GMT20240301-141200_Recording.vtt").write_text(
        VTT_HEAD + cue(1, "Rita Alvarez", "Part two, and here I am."), encoding="utf-8"
    )

    # Two speakers between them, so it is kept.
    assert run(source_root, out, "--skip-single-speaker") == 0
    assert (out / "P53").exists()


def test_a_long_institutional_name_is_one_of_two_speakers(tmp_path, source_root, out):
    """The failure mode was silent: both people collapsed into one label."""
    label = "Bartholomew F. (Jim) Lightning, Brown University, USA"
    make_folder(
        tmp_path,
        VTT_HEAD
        + cue(1, "Ada Lovelace", "Thanks for joining.")
        + cue(2, label, "Glad to help. Everyone calls me Jim.")
        + cue(3, "Ada Lovelace", "Tell me more.")
        + cue(4, label, "Mostly interviews these days."),
        "P60",
    )

    assert run(source_root, out) == 0
    text = read(out, "P60")
    assert "interviewer 1: Thanks for joining." in text
    assert "P60: Glad to help." in text
    assert "Everyone calls me P60." in text
    for leaked in ("Bartholomew", "Lightning", "Jim"):
        assert leaked not in text
    # Affiliation words are not name tokens.
    assert "Brown" not in text  # part of the raw label, replaced wholesale


def test_a_label_the_detector_rejects_is_still_surfaced(tmp_path, source_root, out, capsys):
    """The reviewer must be looser than the detector, or the merge is invisible."""
    make_folder(
        tmp_path,
        VTT_HEAD
        + cue(1, "Ada Lovelace", "Welcome.")
        + cue(2, "Rita Alvarez", "Hello.")
        + cue(3, "Quentin Marchetti-Delacroix del Rio y Santos de la Vega Herrera Ruiz", "Hi."),
        "P61",
    )

    run(source_root, out)
    output = capsys.readouterr().out
    assert "REVIEW" in output
    assert "Quentin" in output


# -- one speaker, nobody identifiable ----------------------------------


def test_a_lone_speaker_becomes_unknown(tmp_path, source_root, out):
    """An in-person recording files the whole room under one name, so that name
    identifies nobody and must not become the participant."""
    make_folder(
        tmp_path,
        VTT_HEAD
        + cue(1, "Sireesh Gururaja", "So tell me about your process.")
        + cue(2, "Sireesh Gururaja", "Honestly I read the whole thing first.")
        + cue(3, "Sireesh Gururaja", "And what breaks down?"),
        "P70",
    )

    assert run(source_root, out) == 0
    text = read(out, "P70")
    assert text.count("unknown:") == 3
    assert "Sireesh" not in text and "Gururaja" not in text
    # Not attributed to the participant, because nothing says it was them.
    assert "P70:" not in text


def test_naming_the_lone_speaker_still_works(tmp_path, source_root, out):
    """Saying who it is overrides the unknown fallback."""
    make_folder(
        tmp_path,
        VTT_HEAD + cue(1, "Ada Lovelace", "One.") + cue(2, "Ada Lovelace", "Two."),
        "P71",
    )

    assert run(source_root, out, "--interviewer", "Ada Lovelace") == 1
    # Every speaker named as an interviewer leaves no participant, and is refused.
    assert not (out / "P71").exists()


def test_two_speakers_are_unaffected_by_the_unknown_rule(folder, source_root, out):
    run(source_root, out)
    text = read(out, "P07", "GMT20240301-140000_Recording.transcript.vtt")
    assert "unknown" not in text
    assert "interviewer 1:" in text and "P07:" in text

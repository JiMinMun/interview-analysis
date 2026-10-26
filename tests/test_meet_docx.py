"""Converting a Google Meet transcript document into WebVTT.

Meet gives a timestamp every five minutes and nothing in between, so these pin
down the parts that are decided rather than measured: the marks land exactly,
the heading is not mistaken for speech, a stray colon is not a new speaker, and
what comes out reads back through the same parser as a Zoom file.
"""

import importlib.util
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from subtitle_search.vtt import parse_vtt

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "meet_docx_to_vtt.py"
_spec = importlib.util.spec_from_file_location("meet_docx_to_vtt", _SCRIPT)
meet = importlib.util.module_from_spec(_spec)
sys.modules["meet_docx_to_vtt"] = meet
_spec.loader.exec_module(meet)


def write_docx(path: Path, paragraphs: list[str]) -> Path:
    """A minimal .docx: one paragraph per entry, a newline becoming a soft break."""
    body = "".join(
        "<w:p>"
        + "<w:r><w:br/></w:r>".join(f"<w:r><w:t>{escape(line)}</w:t></w:r>" for line in text.split("\n"))
        + "</w:p>"
        for text in paragraphs
    )
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return path


MEETING = [
    "Interview - Transcript",
    "Attendees",
    "Speaker 1, Speaker 2",
    "Transcript",
    "00:00:00",
    "Speaker 1: hi nice to meet you",
    "Speaker 2: hi",
    "Speaker 1: Tell me about how you work.",
    "00:05:00",
    "Speaker 2: Mostly alone, at night.",
    "Speaker 1: Why at night?",
    "Meeting ended after 00:07:30 👋",
]


def test_marks_land_exactly_and_the_heading_is_left_out(tmp_path):
    result = meet.convert(meet.docx_lines(write_docx(tmp_path / "t.docx", MEETING)))
    transcript = parse_vtt(result.vtt)

    assert transcript.speakers == ["Speaker 1", "Speaker 2"]
    assert [c.text for c in transcript.cues][0] == "hi nice to meet you"
    assert transcript.cues[0].start == 0
    # The first turn after the mark starts on it, and the one before ends on it.
    assert transcript.cues[2].end == pytest.approx(300)
    assert transcript.cues[3].start == pytest.approx(300)
    assert transcript.cues[-1].end == pytest.approx(450)
    assert result.end_source.startswith('"Meeting ended')
    assert result.dropped == ["Interview - Transcript", "Attendees", "Speaker 1, Speaker 2", "Transcript"]


STAMPED = [
    "Interview - Transcript",
    "00:00",
    "Interviewer: Thanks for joining.",
    "00:04",
    "Alex: Happy to be here.",
    "00:09",
    "Interviewer: Tell me how you start.",
    "00:09",
    "Alex: Quickly.",
    "01:02:03",
    "Interviewer: Last one.",
]


def test_a_stamp_on_every_turn_times_each_turn_by_it():
    transcript = parse_vtt(meet.convert(STAMPED, end=3730.0).vtt)
    assert [(c.speaker, c.start) for c in transcript.cues[:2]] == [
        ("Interviewer", 0),
        ("Alex", pytest.approx(4)),
    ]
    assert transcript.cues[0].end == pytest.approx(4)
    assert transcript.cues[-1].start == pytest.approx(3723)


def test_someone_who_speaks_once_is_still_a_speaker_when_stamped():
    # "Alex" appears once and is a single word, so on its own it would not pass.
    lines = ["00:00", "Interviewer: Hello.", "00:03", "Alex: Hi.", "00:05", "Interviewer: Great."]
    transcript = parse_vtt(meet.convert(lines, end=8.0).vtt)
    assert transcript.speakers == ["Interviewer", "Alex"]


def test_turns_stamped_the_same_second_share_it_rather_than_get_none():
    transcript = parse_vtt(meet.convert(STAMPED, end=3730.0).vtt)
    third, fourth = transcript.cues[2], transcript.cues[3]
    assert third.start == pytest.approx(9)
    assert third.end == fourth.start
    assert fourth.end > fourth.start


@pytest.mark.parametrize(
    "first, second",
    [
        ("[00:01] Interviewer: How do you start?", "[00:05] Alex: Slowly."),
        ("00:01 Interviewer: How do you start?", "00:05 Alex: Slowly."),
        ("00:01 | Interviewer: How do you start?", "00:05 | Alex: Slowly."),
        ("(00:01) Interviewer: How do you start?", "(00:05) Alex: Slowly."),
        ("[00:01]\nInterviewer: How do you start?", "[00:05]\nAlex: Slowly."),
        # The words on the line below the name, as some exports lay it out.
        ("00:01 Interviewer:\nHow do you start?", "00:05 Alex:\nSlowly."),
    ],
)
def test_every_layout_of_a_stamped_turn_reads_the_same(first, second):
    lines = (first + "\n" + second).split("\n")
    transcript = parse_vtt(meet.convert(lines, end=9.0).vtt)
    assert [(c.speaker, c.text, c.start, c.end) for c in transcript.cues] == [
        ("Interviewer", "How do you start?", pytest.approx(1), pytest.approx(5)),
        ("Alex", "Slowly.", pytest.approx(5), pytest.approx(9)),
    ]


def test_speech_that_opens_with_a_time_stays_speech():
    lines = [
        "00:01 Interviewer: When do you start?",
        "00:05 Alex: Early.",
        "10:30 is when the first meeting is.",
        "00:12 Interviewer: Thanks.",
    ]
    transcript = parse_vtt(meet.convert(lines, end=15.0).vtt)
    assert transcript.cues[1].text == "Early. 10:30 is when the first meeting is."


def test_time_is_shared_out_by_how_much_was_said(tmp_path):
    lines = ["00:00:00", "Speaker 1: " + "word " * 5, "Speaker 2: " + "word " * 15, "00:01:00"]
    lines += ["Speaker 1: done", "Speaker 2: done"]
    transcript = parse_vtt(meet.convert(lines, end=70.0).vtt)
    first, second = transcript.cues[0], transcript.cues[1]
    assert (second.end - second.start) == pytest.approx(3 * (first.end - first.start), rel=0.05)


def test_a_colon_in_someones_speech_is_not_a_new_speaker():
    lines = [
        "00:00:00",
        "Speaker 1: What did you write down?",
        "Speaker 2: I wrote it like this.",
        "Note: bring the forms.",
        "Speaker 1: Got it.",
        "Speaker 2: Good.",
    ]
    transcript = parse_vtt(meet.convert(lines, end=60.0).vtt)
    assert transcript.speakers == ["Speaker 1", "Speaker 2"]
    assert transcript.cues[1].text == "I wrote it like this. Note: bring the forms."


def test_soft_breaks_inside_a_paragraph_are_separate_turns(tmp_path):
    path = write_docx(tmp_path / "t.docx", ["00:00:00\nSpeaker 1: one\nSpeaker 2: two"])
    assert meet.docx_lines(path) == ["00:00:00", "Speaker 1: one", "Speaker 2: two"]


def test_a_long_turn_is_cut_into_captions_without_losing_words():
    sentence = " ".join(f"w{i}" for i in range(45)) + "."
    text = "Short one. " + sentence + " Tail end."
    captions = meet.caption_texts(text)
    assert all(len(c.split()) <= meet.MAX_CAPTION_WORDS for c in captions)
    assert " ".join(captions) == text


def test_the_end_falls_back_to_the_recording_then_a_guess():
    lines = ["00:00:00", "Speaker 1: hello there", "Speaker 2: " + "word " * 25]
    lines += ["Speaker 1: right", "Speaker 2: yes"]
    assert meet.convert(lines, recording=90.0).end == 90.0
    guessed = meet.convert(lines)
    assert guessed.end == pytest.approx(29 / meet.WORDS_PER_SECOND)
    assert guessed.end_source.startswith("guessed")


def test_a_session_in_several_videos_is_laid_end_to_end():
    lines = [
        "00:00 Interviewer: First video.",
        "10:00 Alex: Still the first.",
        "00:00 Interviewer: Second video.",
        "04:00 Alex: Still the second.",
        "00:00 Interviewer: Third video.",
        "00:30 Alex: Done.",
    ]
    result = meet.convert(lines, end=1000.0)
    transcript = parse_vtt(result.vtt)
    # Each restart lands one second after the last stamp before it, and the
    # stamps inside a chunk keep their spacing.
    assert [c.start for c in transcript.cues] == pytest.approx([0, 600, 601, 841, 842, 872])
    assert result.restarts == 2


def test_stamps_that_only_repeat_are_not_a_restart():
    result = meet.convert(["00:05 Interviewer: a", "00:05 Alex: b", "00:09 Interviewer: c"], end=12.0)
    assert result.restarts == 0


def test_writes_beside_the_document_and_will_not_overwrite(tmp_path, capsys):
    folder = tmp_path / "raw" / "P01"
    folder.mkdir(parents=True)
    write_docx(folder / "interview.docx", MEETING)

    assert meet.main([str(tmp_path / "raw")]) == 0
    written = folder / "interview.vtt"
    assert parse_vtt(written.read_text()).speakers == ["Speaker 1", "Speaker 2"]

    assert meet.main([str(tmp_path / "raw")]) == 1
    assert "already exists" in capsys.readouterr().out

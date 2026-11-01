"""The library: many recordings at once, and the themes built across them."""

import json

import pytest
from fastapi.testclient import TestClient

from subtitle_search.app import create_app
from subtitle_search.library import (
    AREA_HEAD,
    AREA_PAD,
    CARD_H,
    CARD_W,
    THEMES_FILENAME,
    ThemeStore,
    all_quotes,
    cooccurrence,
    tag_index,
    untagged,
)
from subtitle_search.session import RecordingRegistry, find_recordings

from .test_session import write_mp4

TRANSCRIPT = """WEBVTT

1
00:00:01.000 --> 00:00:06.000
Dana Whitfield: Tell me how you handle transcripts today.

2
00:00:07.000 --> 00:00:13.000
Rafael Ortiz: I read the whole thing, then hunt for where the quote actually is.

3
00:00:14.000 --> 00:00:20.000
Dana Whitfield: And does privacy come into it?

4
00:00:21.000 --> 00:00:27.000
Rafael Ortiz: Constantly. These are participant recordings.
"""


def quote(index: int, tags: list[str], color: str = "amber") -> dict:
    return {
        "id": f"q{index}",
        "text": f"quote number {index}",
        "color": color,
        "note": "",
        "tags": tags,
        "start_cue_id": "c1",
        "end_cue_id": "c1",
        "start_char_offset": 0,
        "end_char_offset": 10,
        "start_time": 7.0 + index,
        "end_time": 12.0 + index,
        "speaker": "Rafael Ortiz",
    }


def make_recording(root, name: str, quotes: list[dict] | None = None, media: bool = True):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "session.vtt").write_text(TRANSCRIPT, encoding="utf-8")
    if media:
        write_mp4(folder / "session.mp4", 120)
    if quotes is not None:
        (folder / "session.highlights.json").write_text(
            json.dumps({"version": 1, "known_tags": [], "highlights": quotes})
        )
    return folder


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "study"
    make_recording(root, "P01", [quote(1, ["trust"]), quote(2, ["timestamps", "tone"])])
    make_recording(root, "P02", [quote(3, ["timestamps"]), quote(4, [])])
    make_recording(root, "P03", [quote(5, ["trust", "timestamps"])])
    return root


@pytest.fixture
def client(library):
    registry = RecordingRegistry()
    registry.add_library(library)
    return TestClient(create_app(registry)), registry


# -- discovery ---------------------------------------------------------


def test_a_folder_of_folders_is_a_library(library):
    found = find_recordings(library)
    assert [p.name for p in found] == ["P01", "P02", "P03"]


def test_a_folder_holding_a_transcript_is_itself_the_recording(library):
    """Pointing at one recording must not be read as a library of one."""
    found = find_recordings(library / "P01")
    assert found == [library / "P01"]


def test_folders_without_transcripts_are_not_recordings(tmp_path):
    root = tmp_path / "study"
    make_recording(root, "P01", [])
    (root / "notes").mkdir()
    (root / "notes" / "scratch.txt").write_text("nothing here")

    assert [p.name for p in find_recordings(root)] == ["P01"]


def test_one_unreadable_folder_does_not_sink_the_library(tmp_path):
    root = tmp_path / "study"
    make_recording(root, "P01", [])
    broken = root / "P02"
    broken.mkdir()
    (broken / "session.vtt").write_text("this is not a transcript at all")

    registry = RecordingRegistry()
    registry.add_library(root)

    assert [r.title for r in registry.list()] == ["P01"]
    assert registry.failures and registry.failures[0][0] == "P02"


def test_a_single_recording_is_not_a_library(tmp_path):
    root = tmp_path / "study"
    folder = make_recording(root, "P01", [])
    registry = RecordingRegistry()
    registry.add_library(folder)
    assert registry.is_library is False


# -- aggregation -------------------------------------------------------


def test_quotes_carry_where_they_came_from(client):
    _, registry = client
    quotes = all_quotes(registry)

    assert len(quotes) == 5
    assert all(":" in q["ref"] for q in quotes)
    assert {q["recording_title"] for q in quotes} == {"P01", "P02", "P03"}


def test_tag_index_counts_recordings_not_just_quotes(client):
    """A tag on many quotes from one person is not the same as a shared theme."""
    _, registry = client
    tags = {entry["tag"]: entry for entry in tag_index(all_quotes(registry))}

    assert tags["timestamps"]["quote_count"] == 3
    assert tags["timestamps"]["recording_count"] == 3
    assert tags["tone"]["quote_count"] == 1
    assert tags["tone"]["recording_count"] == 1


def test_tags_are_ordered_by_how_widely_they_are_shared(client):
    _, registry = client
    order = [entry["tag"] for entry in tag_index(all_quotes(registry))]
    assert order[0] == "timestamps"  # in 3 recordings
    assert order.index("trust") < order.index("tone")


def test_cooccurrence_finds_tags_sharing_a_quote(client):
    _, registry = client
    pairs = cooccurrence(all_quotes(registry))
    found = {(p["a"], p["b"]): p for p in pairs}

    assert ("timestamps", "tone") in found
    assert ("timestamps", "trust") in found
    assert found[("timestamps", "tone")]["count"] == 1
    assert ("trust", "tone") not in found  # never on the same quote


def test_untagged_quotes_are_findable(client):
    _, registry = client
    assert [q["id"] for q in untagged(all_quotes(registry))] == ["q4"]


# -- the API -----------------------------------------------------------


def test_library_endpoint(client):
    api, _ = client
    body = api.get("/api/library").json()

    assert body["is_library"] is True
    assert len(body["recordings"]) == 3
    assert body["quote_count"] == 5
    assert body["untagged_count"] == 1
    assert body["recordings"][0]["parts"]  # so a quote can be played elsewhere


def test_search_spans_every_transcript(client):
    api, _ = client
    body = api.get("/api/library/search", params={"q": "participant recordings"}).json()

    assert body["results"]
    assert {hit["recording_title"] for hit in body["results"]} == {"P01", "P02", "P03"}


def test_library_page_is_served_for_many_recordings(client):
    api, _ = client
    assert "Library" in api.get("/").text
    assert "<title>Themes</title>" in api.get("/themes").text
    assert "<title>Quotes</title>" in api.get("/quotes").text
    assert "id=\"transcript\"" in api.get("/reader").text


def test_the_home_page_is_the_library_even_for_one_recording(tmp_path):
    """One entry point beats a home page that changes shape with the folder count."""
    root = tmp_path / "study"
    folder = make_recording(root, "P01", [])
    registry = RecordingRegistry()
    registry.add_library(folder)

    home = TestClient(create_app(registry)).get("/").text
    assert "<title>Library</title>" in home
    assert 'id="transcript"' not in home


# -- themes ------------------------------------------------------------


def test_theme_round_trip(client, library):
    api, _ = client
    created = api.post("/api/library/themes", json={"title": "Losing the recording"})
    assert created.status_code == 201
    theme = created.json()["theme"]

    quotes = api.get("/api/library/quotes").json()["quotes"]
    ref = quotes[0]["ref"]

    api.post("/api/library/themes/assign", json={"ref": ref, "theme_id": theme["id"]})
    listing = api.get("/api/library/themes").json()

    assert listing["themes"][0]["refs"] == [ref]
    assert listing["placed"] == [ref]
    # Themes live beside the recordings, not inside any one of them.
    assert (library / THEMES_FILENAME).exists()


def test_a_quote_belongs_to_one_theme_at_a_time(client):
    """The board's whole point is forcing the decision a tag list defers."""
    api, _ = client
    first = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    second = api.post("/api/library/themes", json={"title": "Two"}).json()["theme"]
    ref = api.get("/api/library/quotes").json()["quotes"][0]["ref"]

    api.post("/api/library/themes/assign", json={"ref": ref, "theme_id": first["id"]})
    body = api.post(
        "/api/library/themes/assign", json={"ref": ref, "theme_id": second["id"]}
    ).json()

    by_id = {t["id"]: t for t in body["themes"]}
    assert by_id[first["id"]]["refs"] == []
    assert by_id[second["id"]]["refs"] == [ref]


def test_assigning_to_no_theme_returns_a_quote_to_unsorted(client):
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    ref = api.get("/api/library/quotes").json()["quotes"][0]["ref"]

    api.post("/api/library/themes/assign", json={"ref": ref, "theme_id": theme["id"]})
    api.post("/api/library/themes/assign", json={"ref": ref, "theme_id": None})

    assert api.get("/api/library/themes").json()["placed"] == []


def test_renaming_and_deleting_a_theme(client):
    api, _ = client
    theme = api.post("/api/library/themes", json={}).json()["theme"]
    assert theme["title"] == "Untitled theme"

    renamed = api.patch(f"/api/library/themes/{theme['id']}", json={"title": "Trust", "note": "why"})
    assert renamed.json()["theme"]["title"] == "Trust"
    assert renamed.json()["theme"]["note"] == "why"

    assert api.delete(f"/api/library/themes/{theme['id']}").status_code == 200
    assert api.get("/api/library/themes").json()["themes"] == []
    assert api.delete(f"/api/library/themes/{theme['id']}").status_code == 404


def test_a_theme_referring_to_a_deleted_quote_is_pruned(tmp_path):
    """A quote deleted in the reader must not leave a hole nothing accounts for."""
    root = tmp_path / "study"
    make_recording(root, "P01", [quote(1, ["trust"])])
    registry = RecordingRegistry()
    registry.add_library(root)
    api = TestClient(create_app(registry))

    theme = api.post("/api/library/themes", json={"title": "T"}).json()["theme"]
    ref = api.get("/api/library/quotes").json()["quotes"][0]["ref"]
    api.post("/api/library/themes/assign", json={"ref": ref, "theme_id": theme["id"]})

    recording = registry.list()[0]
    recording.store.delete("q1")

    assert api.get("/api/library/themes").json()["themes"][0]["refs"] == []


def test_themes_survive_a_reload(tmp_path, library):
    store = ThemeStore(library / THEMES_FILENAME)
    theme = store.create("Kept")
    store.assign("rec:q1", theme["id"])

    reopened = ThemeStore(library / THEMES_FILENAME)
    assert [t["title"] for t in reopened.list()] == ["Kept"]
    assert reopened.placed_refs() == {"rec:q1"}


def test_unknown_fields_in_the_themes_file_survive(tmp_path):
    path = tmp_path / THEMES_FILENAME
    path.write_text(
        json.dumps(
            {
                "version": 99,
                "future_field": {"keep": "me"},
                "themes": [{"id": "t1", "title": "Kept", "refs": ["a:b"], "future": 1}],
            }
        )
    )

    store = ThemeStore(path)
    store.update("t1", {"note": "added"})

    data = json.loads(path.read_text())
    assert data["future_field"] == {"keep": "me"}
    assert data["themes"][0]["future"] == 1


def test_assigning_to_an_unknown_theme_is_a_404(client):
    api, _ = client
    ref = api.get("/api/library/quotes").json()["quotes"][0]["ref"]
    assert api.post(
        "/api/library/themes/assign", json={"ref": ref, "theme_id": "nope"}
    ).status_code == 404


def test_assigning_without_a_reference_is_a_400(client):
    api, _ = client
    assert api.post("/api/library/themes/assign", json={}).status_code == 400


# -- the canvas ---------------------------------------------------------
#
# The canvas is the same themes on a plane. What these check is the part a
# column list never had to answer: that positions survive, that an area carries
# its quotes when it moves, and that a card -- not a quote -- is the thing being
# placed, which is what lets one quote sit in two themes.


def refs_of(api):
    return [q["ref"] for q in api.get("/api/library/quotes").json()["quotes"]]


def overlapping(cards):
    """Any two cards close enough to hide each other."""
    return any(
        abs(a["x"] - b["x"]) < CARD_W and abs(a["y"] - b["y"]) < CARD_H
        for i, a in enumerate(cards)
        for b in cards[i + 1 :]
    )


def test_the_canvas_page_offers_both_shapes(client):
    api, _ = client
    page = api.get("/themes").text
    assert 'id="view-canvas"' in page
    assert 'id="view-board"' in page
    assert 'data-mode="canvas"' in page


def test_a_themes_file_from_before_the_canvas_is_laid_out_on_it(tmp_path, library):
    """The old file records membership and no positions. It must not open empty.

    Re-sorting work that was already sorted is the one outcome that would make
    the canvas worse than the board it replaces, so the first read places the
    themes and packs each one's quotes inside its area.
    """
    registry = RecordingRegistry()
    registry.add_library(library)
    refs = [q["ref"] for q in all_quotes(registry)]

    path = library / THEMES_FILENAME
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "themes": [
                    {"id": "t1", "title": "Old", "note": "", "color": None, "refs": refs[:3]},
                    {"id": "t2", "title": "Empty", "note": "", "color": None, "refs": []},
                ],
            }
        )
    )

    store = ThemeStore(path)
    themes = {t["id"]: t for t in store.list()}
    assert all(isinstance(themes["t1"][key], float) for key in ("x", "y", "w", "h"))
    # The two areas are laid side by side rather than on top of each other.
    assert themes["t1"]["x"] != themes["t2"]["x"] or themes["t1"]["y"] != themes["t2"]["y"]

    cards = store.cards()
    assert {c["ref"] for c in cards} == set(refs[:3])
    assert all(c["theme_id"] == "t1" for c in cards)
    assert not overlapping(cards)
    # Nothing sits over the area's own title.
    assert all(c["y"] >= AREA_HEAD and c["x"] >= AREA_PAD for c in cards)

    # And it was written down, so the next read is not a second layout.
    again = ThemeStore(path)
    assert [(c["ref"], c["x"], c["y"]) for c in again.cards()] == [
        (c["ref"], c["x"], c["y"]) for c in cards
    ]


def test_placing_a_quote_puts_a_card_where_it_was_dropped(client):
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "Trust"}).json()["theme"]
    ref = refs_of(api)[0]

    body = api.post(
        "/api/library/canvas/place",
        json={"ref": ref, "theme_id": theme["id"], "x": 40, "y": 120},
    ).json()

    assert body["cards"] == [{"ref": ref, "theme_id": theme["id"], "x": 40.0, "y": 120.0}]
    assert body["themes"][0]["refs"] == [ref]
    assert body["placed"] == [ref]
    assert body["on_canvas"] == [ref]


def test_a_card_dropped_on_bare_canvas_is_on_it_without_being_in_a_theme(client):
    """Parked next to an area, not in it -- a quote you have dealt with but not filed.

    The board and the tray disagree about such a quote on purpose: the board
    calls it unsorted, and the tray stops offering it.
    """
    api, _ = client
    ref = refs_of(api)[0]
    body = api.post(
        "/api/library/canvas/place", json={"ref": ref, "theme_id": None, "x": 900, "y": 40}
    ).json()

    assert body["cards"] == [{"ref": ref, "theme_id": None, "x": 900.0, "y": 40.0}]
    assert body["placed"] == []
    assert body["on_canvas"] == [ref]


def test_dragging_a_card_between_areas_moves_it(client):
    api, _ = client
    first = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    second = api.post("/api/library/themes", json={"title": "Two"}).json()["theme"]
    ref = refs_of(api)[0]

    api.post(
        "/api/library/canvas/place",
        json={"ref": ref, "theme_id": first["id"], "x": 20, "y": 100},
    )
    body = api.post(
        "/api/library/canvas/place",
        json={
            "ref": ref,
            "theme_id": second["id"],
            "x": 30,
            "y": 110,
            "moved_from": first["id"],
        },
    ).json()

    assert len(body["cards"]) == 1
    assert body["cards"][0]["theme_id"] == second["id"]
    by_id = {t["id"]: t for t in body["themes"]}
    assert by_id[first["id"]]["refs"] == []
    assert by_id[second["id"]]["refs"] == [ref]


def test_the_same_quote_can_be_pinned_in_two_areas(client):
    """A card is one appearance of a quote, so a quote can appear twice.

    Saying nothing about where the card came from is the copy gesture -- what you
    would do with a photocopier and two walls.
    """
    api, _ = client
    first = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    second = api.post("/api/library/themes", json={"title": "Two"}).json()["theme"]
    ref = refs_of(api)[0]

    api.post(
        "/api/library/canvas/place",
        json={"ref": ref, "theme_id": first["id"], "x": 20, "y": 100},
    )
    body = api.post(
        "/api/library/canvas/place",
        json={"ref": ref, "theme_id": second["id"], "x": 20, "y": 100},
    ).json()

    assert len(body["cards"]) == 2
    by_id = {t["id"]: t for t in body["themes"]}
    assert by_id[first["id"]]["refs"] == [ref]
    assert by_id[second["id"]]["refs"] == [ref]
    # One quote, however many cards: the counts are of quotes.
    assert body["placed"] == [ref]
    assert body["on_canvas"] == [ref]


def test_only_one_card_per_quote_per_area(client):
    """Placing into somewhere it already is moves it, rather than stacking it."""
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    ref = refs_of(api)[0]

    api.post(
        "/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"], "x": 20, "y": 100}
    )
    body = api.post(
        "/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"], "x": 60, "y": 200}
    ).json()

    assert body["cards"] == [{"ref": ref, "theme_id": theme["id"], "x": 60.0, "y": 200.0}]


def test_putting_a_card_away_leaves_the_other_copies(client):
    api, _ = client
    first = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    second = api.post("/api/library/themes", json={"title": "Two"}).json()["theme"]
    ref = refs_of(api)[0]
    for theme in (first, second):
        api.post(
            "/api/library/canvas/place",
            json={"ref": ref, "theme_id": theme["id"], "x": 20, "y": 100},
        )

    body = api.post(
        "/api/library/canvas/unplace", json={"ref": ref, "theme_id": first["id"]}
    ).json()

    assert [c["theme_id"] for c in body["cards"]] == [second["id"]]
    assert body["on_canvas"] == [ref]


def test_a_quote_placed_with_no_position_lands_clear_of_what_is_there(client):
    """The board has no coordinates to offer, and must not stack cards blind.

    Free placement means a slot can be empty of any card's corner and still be
    entirely underneath one, so the search is for a clear box rather than an
    unused grid point.
    """
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    refs = refs_of(api)[:3]

    # Two cards placed by hand, deliberately off the grid.
    api.post(
        "/api/library/canvas/place",
        json={"ref": refs[0], "theme_id": theme["id"], "x": 20, "y": 90},
    )
    api.post(
        "/api/library/canvas/place",
        json={"ref": refs[1], "theme_id": theme["id"], "x": 250, "y": 95},
    )
    # And one arriving from the board, saying nothing about where.
    body = api.post(
        "/api/library/canvas/place", json={"ref": refs[2], "theme_id": theme["id"]}
    ).json()

    assert not overlapping(body["cards"])
    assert len(body["cards"]) == 3


def test_moving_an_area_carries_its_quotes(client):
    """Card positions are relative to the area, which is what makes this free.

    Moving an area is two numbers changing. No card can be left behind, because
    nothing about the cards is touched.
    """
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    ref = refs_of(api)[0]
    api.post(
        "/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"], "x": 40, "y": 120}
    )

    moved = api.post(
        "/api/library/canvas/reshape", json={"theme_id": theme["id"], "x": 1500, "y": 900}
    ).json()["theme"]
    assert (moved["x"], moved["y"]) == (1500.0, 900.0)

    cards = api.get("/api/library/themes").json()["cards"]
    assert cards == [{"ref": ref, "theme_id": theme["id"], "x": 40.0, "y": 120.0}]


def test_shrinking_an_area_pulls_its_quotes_back_inside(client):
    """A quote does not stop being in a theme because the box was dragged in."""
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    ref = refs_of(api)[0]
    api.post(
        "/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"], "x": 260, "y": 240}
    )

    small = api.post(
        "/api/library/canvas/reshape", json={"theme_id": theme["id"], "w": 280, "h": 240}
    ).json()["theme"]

    body = api.get("/api/library/themes").json()
    card = body["cards"][0]
    assert body["themes"][0]["refs"] == [ref]
    assert AREA_PAD <= card["x"] <= small["w"] - AREA_PAD - CARD_W
    assert AREA_HEAD <= card["y"] <= max(AREA_HEAD, small["h"] - AREA_PAD - CARD_H)


def test_an_area_cannot_be_shrunk_smaller_than_a_card(client):
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    tiny = api.post(
        "/api/library/canvas/reshape", json={"theme_id": theme["id"], "w": 1, "h": 1}
    ).json()["theme"]
    assert tiny["w"] >= CARD_W and tiny["h"] >= CARD_H


def test_tidying_packs_an_area_and_grows_it_to_fit(client):
    """The way back from a mess, per area, without undoing the sorting."""
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    refs = refs_of(api)[:4]
    # All on the same spot, which free placement allows and nobody wants.
    for ref in refs:
        api.post(
            "/api/library/canvas/place",
            json={"ref": ref, "theme_id": theme["id"], "x": 30, "y": 100},
        )

    body = api.post("/api/library/canvas/tidy", json={"theme_id": theme["id"]}).json()
    cards = body["cards"]
    theme = body["themes"][0]

    assert len(cards) == len(refs)
    assert not overlapping(cards)
    assert all(c["y"] + CARD_H <= theme["h"] - AREA_PAD + 0.01 for c in cards)
    assert set(theme["refs"]) == set(refs)


def named(index: int, speaker: str, start: float, tags=()) -> dict:
    """A quote with a speaker and a time worth sorting on."""
    return {
        **quote(index, list(tags)),
        "speaker": speaker,
        "start_time": start,
        "end_time": start + 4,
    }


def test_tidying_orders_by_speaker_then_by_time(tmp_path):
    """One voice at a time, each running in the order it was said.

    A theme read that way is one you can argue with: the same person's remarks
    sit together, and the place where somebody else takes over is visible.
    """
    root = tmp_path / "study"
    make_recording(
        root,
        "P01",
        [
            named(1, "Rafael Ortiz", 90.0),
            named(2, "Dana Whitfield", 30.0),
            named(3, "Rafael Ortiz", 10.0),
            named(4, "Dana Whitfield", 60.0),
        ],
    )
    registry = RecordingRegistry()
    registry.add_library(root)
    api = TestClient(create_app(registry))

    theme = api.post("/api/library/themes", json={"title": "T"}).json()["theme"]
    quotes = api.get("/api/library/quotes").json()["quotes"]
    by_ref = {q["ref"]: q for q in quotes}
    # Placed in a deliberately unhelpful order, and scattered.
    for offset, ref in enumerate(reversed([q["ref"] for q in quotes])):
        api.post(
            "/api/library/canvas/place",
            json={"ref": ref, "theme_id": theme["id"], "x": 200 - offset * 30, "y": 300 - offset * 20},
        )

    body = api.post("/api/library/canvas/tidy", json={"theme_id": theme["id"]}).json()
    packed = sorted(body["cards"], key=lambda c: (c["y"], c["x"]))
    assert [
        (by_ref[c["ref"]]["speaker"], by_ref[c["ref"]]["start_time"]) for c in packed
    ] == [
        ("Dana Whitfield", 30.0),
        ("Dana Whitfield", 60.0),
        ("Rafael Ortiz", 10.0),
        ("Rafael Ortiz", 90.0),
    ]


def test_a_quote_nobody_is_credited_with_packs_last(tmp_path):
    """Unattributed quotes are the ones to fix, not the ones to read first."""
    root = tmp_path / "study"
    make_recording(
        root,
        "P01",
        [
            {**named(1, "", 5.0), "speaker": None},
            named(2, "Zoe Nakamura", 99.0),
            named(3, "Dana Whitfield", 50.0),
        ],
    )
    registry = RecordingRegistry()
    registry.add_library(root)
    api = TestClient(create_app(registry))

    theme = api.post("/api/library/themes", json={"title": "T"}).json()["theme"]
    by_ref = {q["ref"]: q for q in api.get("/api/library/quotes").json()["quotes"]}
    for ref in by_ref:
        api.post("/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"]})

    body = api.post("/api/library/canvas/tidy", json={"theme_id": theme["id"]}).json()
    packed = sorted(body["cards"], key=lambda c: (c["y"], c["x"]))
    assert [by_ref[c["ref"]]["speaker"] for c in packed] == [
        "Dana Whitfield",
        "Zoe Nakamura",
        None,
    ]


def test_tidying_twice_changes_nothing(client):
    """The order comes from the quotes, so it does not depend on where they were."""
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "T"}).json()["theme"]
    for ref in refs_of(api)[:4]:
        api.post("/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"]})

    once = api.post("/api/library/canvas/tidy", json={"theme_id": theme["id"]}).json()["cards"]
    twice = api.post("/api/library/canvas/tidy", json={"theme_id": theme["id"]}).json()["cards"]
    assert once == twice


def test_positions_are_saved_in_one_batch(client):
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    refs = refs_of(api)[:2]
    for ref in refs:
        api.post(
            "/api/library/canvas/place",
            json={"ref": ref, "theme_id": theme["id"], "x": 20, "y": 100},
        )

    body = api.post(
        "/api/library/canvas/positions",
        json={
            "moves": [
                {"ref": refs[0], "theme_id": theme["id"], "x": 100, "y": 200},
                {"ref": refs[1], "theme_id": theme["id"], "x": 30, "y": 100},
                {"ref": "nothing:here", "theme_id": theme["id"], "x": 1, "y": 2},
            ]
        },
    ).json()

    positions = {c["ref"]: (c["x"], c["y"]) for c in body["cards"]}
    assert positions == {refs[0]: (100.0, 200.0), refs[1]: (30.0, 100.0)}


def test_a_card_cannot_be_repositioned_outside_its_own_area(client):
    """The invariant is held at every write, not only checked on the way in."""
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    ref = refs_of(api)[0]
    api.post(
        "/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"], "x": 20, "y": 100}
    )

    body = api.post(
        "/api/library/canvas/positions",
        json={"moves": [{"ref": ref, "theme_id": theme["id"], "x": 9000, "y": 9000}]},
    ).json()

    card, area = body["cards"][0], body["themes"][0]
    assert card["x"] == area["w"] - AREA_PAD - CARD_W
    assert card["y"] == area["h"] - AREA_PAD - CARD_H


def test_an_area_out_of_room_grows_rather_than_stacking(client):
    """Squeezing an arriving card back inside would put it on top of something."""
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    height = theme["h"]

    for ref in refs_of(api):
        api.post("/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"]})
    body = api.get("/api/library/themes").json()

    grown = body["themes"][0]
    assert grown["h"] > height
    assert not overlapping(body["cards"])
    assert all(c["y"] + CARD_H <= grown["h"] - AREA_PAD + 0.01 for c in body["cards"])


def test_nonsense_coordinates_cannot_strand_a_card(client):
    """A bad number must not put a card where no amount of panning finds it."""
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    ref = refs_of(api)[0]

    body = api.post(
        "/api/library/canvas/place",
        json={"ref": ref, "theme_id": theme["id"], "x": "over there", "y": 1e12},
    ).json()

    # Unusable x falls back to the free slot it would have got with no position
    # at all; a wild y lands inside the area rather than a mile below it.
    card = body["cards"][0]
    area = body["themes"][0]
    assert (card["x"], card["y"]) == (
        AREA_PAD,
        max(AREA_HEAD, area["h"] - AREA_PAD - CARD_H),
    )


def test_deleting_an_area_returns_its_quotes_to_the_tray(client):
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "One"}).json()["theme"]
    ref = refs_of(api)[0]
    api.post(
        "/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"], "x": 20, "y": 100}
    )

    api.delete(f"/api/library/themes/{theme['id']}")
    body = api.get("/api/library/themes").json()

    assert body["cards"] == []
    assert body["on_canvas"] == []


def test_an_area_can_be_recreated_with_its_arrangement(client):
    """What undo needs: the box, the note and where each quote sat, in one call.

    The arrangement inside an area is the part that took the time, so an undo
    that restored the title and scrambled the contents would not be an undo.
    """
    api, _ = client
    refs = refs_of(api)[:2]
    restored = api.post(
        "/api/library/themes",
        json={
            "title": "Put back",
            "note": "why it matters",
            "box": {"x": 300, "y": 400, "w": 600, "h": 500},
            "cards": [
                {"ref": refs[0], "x": 20, "y": 100},
                {"ref": refs[1], "x": 300, "y": 260},
            ],
        },
    ).json()

    theme = restored["theme"]
    assert (theme["x"], theme["y"], theme["w"], theme["h"]) == (300.0, 400.0, 600.0, 500.0)
    assert theme["note"] == "why it matters"
    assert set(theme["refs"]) == set(refs)
    assert {(c["ref"], c["x"], c["y"]) for c in restored["cards"]} == {
        (refs[0], 20.0, 100.0),
        (refs[1], 300.0, 260.0),
    }


def test_a_new_area_made_away_from_the_canvas_is_still_findable_on_it(client):
    """A theme added on the board must not land underneath an existing area."""
    api, _ = client
    boxes = []
    for index in range(4):
        theme = api.post("/api/library/themes", json={"title": f"T{index}"}).json()["theme"]
        boxes.append((theme["x"], theme["y"], theme["w"], theme["h"]))

    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert not (
                a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
            ), "two areas were created on top of each other"


def test_deleting_a_quote_removes_its_card_too(tmp_path):
    """A card with nothing behind it would be a blank rectangle nobody can move."""
    root = tmp_path / "study"
    make_recording(root, "P01", [quote(1, ["trust"]), quote(2, [])])
    registry = RecordingRegistry()
    registry.add_library(root)
    api = TestClient(create_app(registry))

    theme = api.post("/api/library/themes", json={"title": "T"}).json()["theme"]
    for ref in refs_of(api):
        api.post(
            "/api/library/canvas/place",
            json={"ref": ref, "theme_id": theme["id"], "x": 20, "y": 100},
        )

    registry.list()[0].store.delete("q1")
    body = api.get("/api/library/themes").json()

    assert len(body["cards"]) == 1
    assert body["cards"][0]["ref"].endswith(":q2")
    assert body["themes"][0]["refs"] == body["on_canvas"]


def test_a_card_left_over_an_areas_title_is_pulled_off_it(tmp_path):
    """The theme's own name is the one thing that must always be readable.

    Dragging and resizing both clamp, so this is for what they cannot reach: a
    hand-edited file, or a position from a version that clamped differently.
    """
    path = tmp_path / THEMES_FILENAME
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "themes": [
                    {
                        "id": "t1", "title": "Kept", "note": "", "color": None,
                        "refs": ["a:b"], "x": 0, "y": 0, "w": 520, "h": 400,
                    }
                ],
                "cards": [{"ref": "a:b", "theme_id": "t1", "x": 0, "y": 0}],
            }
        )
    )

    card = ThemeStore(path).cards()[0]
    assert (card["x"], card["y"]) == (AREA_PAD, AREA_HEAD)
    assert json.loads(path.read_text())["cards"][0]["y"] == AREA_HEAD


def test_the_board_and_the_canvas_are_one_grouping(client):
    """A theme sorted on either shape is sorted on the other.

    They are two shapes of one file, not two groupings, so the board's move is
    the canvas's place -- and this is the test that would fail if a second store
    ever crept in.
    """
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "Shared"}).json()["theme"]
    ref = refs_of(api)[0]

    # Sorted the board's way: no coordinates, one theme at a time.
    api.post("/api/library/themes/assign", json={"ref": ref, "theme_id": theme["id"]})
    body = api.get("/api/library/themes").json()

    assert body["themes"][0]["refs"] == [ref]
    assert [c["ref"] for c in body["cards"]] == [ref]
    assert body["on_canvas"] == [ref]


def test_a_lassoed_theme_arrives_on_the_canvas_too(client):
    """The map's "make a theme from these" has to answer with the whole canvas."""
    api, _ = client
    refs = refs_of(api)[:3]
    body = api.post(
        "/api/library/themes/from-refs", json={"title": "Lassoed", "refs": refs}
    ).json()

    assert set(body["theme"]["refs"]) == set(refs)
    assert {c["ref"] for c in body["cards"]} == set(refs)
    assert not overlapping(body["cards"])


def test_canvas_calls_against_an_unknown_theme_are_404s(client):
    api, _ = client
    ref = refs_of(api)[0]
    assert api.post(
        "/api/library/canvas/place", json={"ref": ref, "theme_id": "nope", "x": 0, "y": 0}
    ).status_code == 404
    assert api.post(
        "/api/library/canvas/reshape", json={"theme_id": "nope", "x": 0}
    ).status_code == 404
    assert api.post("/api/library/canvas/tidy", json={"theme_id": "nope"}).status_code == 404


def test_canvas_calls_missing_what_they_act_on_are_400s(client):
    api, _ = client
    assert api.post("/api/library/canvas/place", json={"x": 0, "y": 0}).status_code == 400
    assert api.post("/api/library/canvas/unplace", json={}).status_code == 400
    assert api.post("/api/library/canvas/reshape", json={"x": 0}).status_code == 400
    assert api.post("/api/library/canvas/tidy", json={}).status_code == 400
    assert api.post("/api/library/canvas/positions", json={"moves": "no"}).status_code == 400


def test_the_canvas_survives_a_reload(tmp_path, library):
    store = ThemeStore(library / THEMES_FILENAME)
    theme = store.create("Kept", box={"x": 700, "y": 800})
    store.place("rec:q1", theme["id"], 40, 120)
    store.place("rec:q1", None, 2000, 300)

    reopened = ThemeStore(library / THEMES_FILENAME)
    kept = reopened.list()[0]
    assert (kept["x"], kept["y"]) == (700.0, 800.0)
    assert kept["refs"] == ["rec:q1"]
    assert sorted(
        (c["theme_id"] or "", c["x"], c["y"]) for c in reopened.cards()
    ) == [("", 2000.0, 300.0), (theme["id"], 40.0, 120.0)]
    assert reopened.placed_refs() == {"rec:q1"}
    assert reopened.on_canvas_refs() == {"rec:q1"}


def test_an_area_can_be_rolled_up_to_its_title(client):
    """Rolling up hides quotes; it must not move or forget any of them.

    A study's themes are not all live at once, and a finished one taking a
    screenful of plane is a finished one in the way. But it is a drawing
    decision, so what is inside keeps its size, its arrangement and its
    membership -- the theme is closed, not shut.
    """
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "Done with"}).json()["theme"]
    assert theme["collapsed"] is False

    ref = refs_of(api)[0]
    api.post(
        "/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"], "x": 40, "y": 120}
    )

    rolled = api.patch(
        f"/api/library/themes/{theme['id']}", json={"collapsed": True}
    ).json()["theme"]
    assert rolled["collapsed"] is True
    # The stored box is untouched, so opening it again is exact.
    assert (rolled["w"], rolled["h"]) == (theme["w"], theme["h"])

    body = api.get("/api/library/themes").json()
    assert body["themes"][0]["refs"] == [ref]
    assert body["cards"] == [{"ref": ref, "theme_id": theme["id"], "x": 40.0, "y": 120.0}]

    opened = api.patch(
        f"/api/library/themes/{theme['id']}", json={"collapsed": False}
    ).json()["theme"]
    assert opened["collapsed"] is False


def test_a_quote_can_still_be_dropped_on_a_rolled_up_area(client):
    """Closed, not shut: it takes quotes and files them where they will be found."""
    api, _ = client
    theme = api.post("/api/library/themes", json={"title": "Rolled"}).json()["theme"]
    first, second = refs_of(api)[:2]
    api.post(
        "/api/library/canvas/place", json={"ref": first, "theme_id": theme["id"], "x": 14, "y": 84}
    )
    api.patch(f"/api/library/themes/{theme['id']}", json={"collapsed": True})

    # Nothing is said about position: there is nowhere visible to name one.
    body = api.post(
        "/api/library/canvas/place", json={"ref": second, "theme_id": theme["id"]}
    ).json()

    assert set(body["themes"][0]["refs"]) == {first, second}
    assert not overlapping(body["cards"])


def test_being_rolled_up_survives_a_reload(tmp_path, library):
    store = ThemeStore(library / THEMES_FILENAME)
    theme = store.create("Kept")
    store.update(theme["id"], {"collapsed": True})

    reopened = ThemeStore(library / THEMES_FILENAME).list()[0]
    assert reopened["collapsed"] is True


def test_a_themes_file_from_before_rolling_up_reads_as_open(tmp_path):
    """Absent is open. A theme nobody has closed is not closed."""
    path = tmp_path / THEMES_FILENAME
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "themes": [
                    {
                        "id": "t1", "title": "Old", "note": "", "color": None, "refs": [],
                        "x": 0, "y": 0, "w": 520, "h": 400,
                    }
                ],
                "cards": [],
            }
        )
    )
    assert ThemeStore(path).list()[0]["collapsed"] is False
    assert json.loads(path.read_text())["themes"][0]["collapsed"] is False


def test_a_file_from_a_later_version_is_not_downgraded(tmp_path):
    """Unknown fields survive, and so does the claim about which version wrote it."""
    path = tmp_path / THEMES_FILENAME
    path.write_text(
        json.dumps(
            {
                "version": 99,
                "future_field": {"keep": "me"},
                "themes": [
                    {
                        "id": "t1", "title": "Kept", "refs": ["a:b"], "future": 1,
                        "x": 10, "y": 20, "w": 520, "h": 400,
                    }
                ],
                "cards": [{"ref": "a:b", "theme_id": "t1", "x": 14, "y": 84, "future": 2}],
            }
        )
    )

    ThemeStore(path).update("t1", {"note": "added"})
    data = json.loads(path.read_text())

    assert data["version"] == 99
    assert data["future_field"] == {"keep": "me"}
    assert data["themes"][0]["future"] == 1
    assert data["cards"][0]["future"] == 2


# -- the tag vocabulary ------------------------------------------------


def test_vocabulary_spans_every_recording(client):
    """The point of it: a tag coined in P01 is offered while tagging in P03."""
    api, registry = client
    from subtitle_search.library import vocabulary

    tags = {entry["tag"]: entry for entry in vocabulary(registry)}
    assert set(tags) == {"trust", "timestamps", "tone"}
    assert tags["timestamps"]["recording_count"] == 3
    assert tags["timestamps"]["quote_count"] == 3
    assert tags["tone"]["recording_count"] == 1


def test_vocabulary_is_ordered_by_how_established_a_tag_is(client):
    api, registry = client
    from subtitle_search.library import vocabulary

    order = [entry["tag"] for entry in vocabulary(registry)]
    assert order[0] == "timestamps"  # in every recording
    assert order.index("trust") < order.index("tone")


def test_vocabulary_keeps_a_tag_whose_quotes_all_lost_it(client):
    """Otherwise a code you stopped using stops being suggested, and gets
    reinvented under a new name a fortnight later."""
    api, registry = client
    from subtitle_search.library import vocabulary

    recording = next(r for r in registry.list() if r.title == "P01")
    quote = recording.store.list()[0]

    # Applied through the store, which is what records it as history.
    recording.store.update(quote["id"], {"tags": ["provisional"]})
    recording.store.update(quote["id"], {"tags": []})

    entry = next((e for e in vocabulary(registry) if e["tag"] == "provisional"), None)
    assert entry is not None, "a tag used once should stay in the vocabulary"
    assert entry["quote_count"] == 0
    assert entry["recording_count"] == 0

    # And it is still offered while typing, unlike the filter list.
    tags = [e["tag"] for e in api.get("/api/library/vocabulary").json()["tags"]]
    assert "provisional" in tags


def test_vocabulary_endpoint(client):
    api, _ = client
    body = api.get("/api/library/vocabulary").json()

    assert [entry["tag"] for entry in body["tags"]][0] == "timestamps"
    assert all({"tag", "quote_count", "recording_count"} <= set(e) for e in body["tags"])


def test_vocabulary_of_an_untagged_library_is_empty(tmp_path):
    root = tmp_path / "study"
    make_recording(root, "P01", [])
    registry = RecordingRegistry()
    registry.add_library(root)

    from subtitle_search.library import vocabulary

    assert vocabulary(registry) == []


# -- renaming and deleting codes ---------------------------------------


def test_renaming_a_code_reaches_every_recording(client):
    api, registry = client
    body = api.post("/api/library/tags/rename", json={"from": "timestamps", "to": "finding the moment"}).json()

    assert body["quote_count"] == 3
    assert body["recording_count"] == 3
    tags = {t for q in all_quotes(registry) for t in q["tags"]}
    assert "timestamps" not in tags
    assert "finding the moment" in tags


def test_renaming_onto_an_existing_code_merges_them(client):
    api, registry = client
    api.post("/api/library/tags/rename", json={"from": "timestamps", "to": "trust"})

    by_id = {q["id"]: q["tags"] for q in all_quotes(registry)}
    assert by_id["q5"] == ["trust"]  # carried both; keeps one
    assert by_id["q2"] == ["trust", "tone"]


def test_a_renamed_code_leaves_no_trace_in_the_vocabulary(client):
    """The old name would otherwise keep being offered and drift back into use."""
    api, registry = client
    recording = next(r for r in registry.list() if r.title == "P01")
    recording.store.update("q1", {"tags": ["provisional"]})
    recording.store.update("q1", {"tags": []})

    api.post("/api/library/tags/rename", json={"from": "provisional", "to": "settled"})

    tags = [e["tag"] for e in api.get("/api/library/vocabulary").json()["tags"]]
    assert "provisional" not in tags
    assert "settled" in tags


def test_a_rename_is_written_to_disk(client, library):
    api, _ = client
    api.post("/api/library/tags/rename", json={"from": "tone", "to": "register"})

    data = json.loads((library / "P01" / "session.highlights.json").read_text())
    assert data["highlights"][1]["tags"] == ["timestamps", "register"]
    assert "tone" not in data["known_tags"]


def test_deleting_a_code_takes_it_off_every_quote(client, library):
    api, registry = client
    body = api.post("/api/library/tags/delete", json={"tag": "trust"}).json()

    assert body["quote_count"] == 2
    assert body["recording_count"] == 2
    by_id = {q["id"]: q["tags"] for q in all_quotes(registry)}
    assert by_id["q1"] == []
    assert by_id["q5"] == ["timestamps"]
    tags = [e["tag"] for e in api.get("/api/library/vocabulary").json()["tags"]]
    assert "trust" not in tags
    data = json.loads((library / "P03" / "session.highlights.json").read_text())
    assert "trust" not in data["known_tags"]


def test_renaming_or_deleting_needs_a_name(client):
    api, _ = client
    assert api.post("/api/library/tags/rename", json={"from": "trust", "to": "  "}).status_code == 400
    assert api.post("/api/library/tags/delete", json={}).status_code == 400

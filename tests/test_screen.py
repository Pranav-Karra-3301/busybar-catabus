"""Board-rendering tests for the held/late treatment: a delayed bus keeps
the normal board (red minutes + "+N" tag) instead of a takeover plate, and
the element-id set is identical either way so late<->on-time flips never
force a canvas clear."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "apps" / "catabus"))
import app  # noqa: E402

ASSETS = {"V": {"bullet_name": "bullet_V.png"}}


def arrivals(mins=5, is_last=False):
    return [(time.time() + mins * 60, "V", "trip1", 0, True, is_last)]


def by_id(els):
    return {e["id"]: e for e in els}


def test_on_time_board_is_white_with_parked_late_tag():
    els = by_id(app.build_screen({}, ASSETS, arrivals(), 0))
    assert els["num"]["color"] == app.WHITE
    assert els["unit"]["color"] == app.WHITE
    assert els["late"]["y"] < 0


def test_late_board_reds_the_minutes_and_shows_plus_n():
    els = by_id(app.build_screen({}, ASSETS, arrivals(), 0,
                                 late_secs=6 * 60))
    assert els["num"]["color"] == app.RED
    assert els["unit"]["color"] == app.RED
    assert els["late"]["text"] == "+6"
    assert els["late"]["y"] == 0


def test_late_outranks_last_for_the_tag_slot():
    els = by_id(app.build_screen({}, ASSETS, arrivals(is_last=True), 0,
                                 late_secs=5 * 60))
    assert els["late"]["y"] == 0
    assert els["last"]["y"] < 0
    # and LAST returns once the bus is back on time
    els = by_id(app.build_screen({}, ASSETS, arrivals(is_last=True), 0))
    assert els["last"]["y"] == 0
    assert els["late"]["y"] < 0


def test_now_case_reds_the_word_but_skips_the_tag():
    els = by_id(app.build_screen({}, ASSETS, arrivals(mins=0), 0,
                                 late_secs=4 * 60))
    assert els["num"]["text"] == "NOW"
    assert els["num"]["color"] == app.RED
    assert els["late"]["y"] < 0


def test_element_id_set_identical_late_or_not():
    on_time = {e["id"] for e in app.build_screen({}, ASSETS, arrivals(), 0)}
    late = {e["id"] for e in app.build_screen({}, ASSETS, arrivals(), 0,
                                              late_secs=300)}
    assert on_time == late

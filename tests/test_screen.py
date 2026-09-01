"""Board-rendering tests for the held/late treatment and the walk filter.
A delayed bus keeps the normal board — text stays WHITE, the shaded red
late-glow plate slides in under the minutes, a small "+N" takes the tag
slot — and the element-id set is identical either way so late<->on-time
flips never force a canvas clear. The walk filter hides buses that depart
sooner than the walk to their stop."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "apps" / "catabus"))
import app  # noqa: E402

ASSETS = {"V": {"bullet_name": "bullet_V.png"}}
GLOW = "late_glow.png"


def arrivals(mins=5, is_last=False):
    return [(time.time() + mins * 60, "V", "trip1", 0, True, is_last)]


def by_id(els):
    return {e["id"]: e for e in els}


def board(late_secs=0, mins=5, is_last=False):
    return by_id(app.build_screen({}, ASSETS, arrivals(mins, is_last), 0,
                                  late_secs=late_secs, late_plate=GLOW))


def test_on_time_board_white_glow_parked():
    els = board()
    assert els["num"]["color"] == app.WHITE
    assert els["unit"]["color"] == app.WHITE
    assert els["lateglow"]["y"] < 0
    assert els["late"]["y"] < 0


def test_late_board_glow_in_text_still_white():
    els = board(late_secs=6 * 60)
    assert els["num"]["color"] == app.WHITE
    assert els["unit"]["color"] == app.WHITE
    assert els["lateglow"]["y"] == 0
    assert els["late"]["text"] == "+6"
    assert els["late"]["y"] == 0


def test_glow_sits_under_everything():
    ids = [e["id"] for e in app.build_screen(
        {}, ASSETS, arrivals(), 0, late_secs=300, late_plate=GLOW)]
    assert ids[0] == "lateglow"  # first created = lowest z on the device


def test_late_outranks_last_for_the_tag_slot():
    els = board(late_secs=5 * 60, is_last=True)
    assert els["late"]["y"] == 0
    assert els["last"]["y"] < 0
    els = board(is_last=True)
    assert els["last"]["y"] == 0
    assert els["late"]["y"] < 0


def test_now_case_keeps_glow_skips_tag():
    els = board(late_secs=4 * 60, mins=0)
    assert els["num"]["text"] == "NOW"
    assert els["lateglow"]["y"] == 0
    assert els["late"]["y"] < 0


def test_element_id_set_identical_late_or_not():
    assert set(board()) == set(board(late_secs=300))


def test_no_glow_element_without_status_assets():
    els = by_id(app.build_screen({}, ASSETS, arrivals(), 0))
    assert "lateglow" not in els


def test_parse_walk():
    assert app.parse_walk("V:2,VE:4,NV:3") == {"V": 2, "VE": 4, "NV": 3}
    assert app.parse_walk(" v : 2 , junk, X:-1, Y:zzz") == {"V": 2, "X": 0}
    assert app.parse_walk("") == {}


def test_catchable_hides_buses_inside_the_walk():
    now = 1_000_000.0
    walk = {"V": 2, "VE": 4}
    mk = lambda mins, route: (now + mins * 60, route, f"t{route}{mins}",
                              0, True, False)
    buses = [mk(1, "V"), mk(2, "V"), mk(3, "VE"), mk(4, "VE"), mk(0, "NV")]
    kept = app.catchable(buses, walk, now=now)
    # V@1 is inside its 2-min walk, VE@3 inside its 4-min walk; NV unlisted
    # walks 0 and always shows
    assert [(b[1], int((b[0] - now) // 60)) for b in kept] == [
        ("V", 2), ("VE", 4), ("NV", 0)]
    assert app.catchable(buses, {}, now=now) == buses

"""Regression tests for the 2026-09-01 audit fixes: unknown-route art,
status-WS input decoding (buttons/switch/encoder), alert classification
order, the "other" alert page, and the soonest-bus-only flash policy."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "apps" / "catabus"))
import app  # noqa: E402


# ---------------------------------------------------------------- proto enc

def _tag(field, wire):
    return bytes([(field << 3) | wire])


def _varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _len_field(field, payload):
    return _tag(field, 2) + _varint(len(payload)) + payload


def _state(*input_events):
    updates = b"".join(_len_field(2, _len_field(11, ev))
                       for ev in input_events)
    return updates


def test_input_events_decodes_all_kinds_in_one_frame():
    zig_plus1 = _len_field(3, _tag(1, 0) + _varint(2))     # delta +1
    zig_minus1 = _len_field(3, _tag(1, 0) + _varint(1))    # delta -1
    ok_press = _len_field(1, b"")                          # all defaults
    back_release = _len_field(1, _tag(1, 0) + _varint(1)
                              + _tag(2, 0) + _varint(1))
    start_press = _len_field(1, _tag(1, 0) + _varint(2))
    switch_off = _len_field(2, _tag(1, 0) + _varint(2))
    frame = _state(zig_plus1, ok_press, back_release,
                   start_press, switch_off, zig_minus1)
    assert app.input_events(frame) == [
        ("encoder", 1, None),
        ("button", app.BTN_OK, app.ACT_PRESS),
        ("button", app.BTN_BACK, app.ACT_RELEASE),
        ("button", app.BTN_START, app.ACT_PRESS),
        ("switch", 2, None),
        ("encoder", -1, None),
    ]


# ---------------------------------------------------------------- art layer

def test_unknown_route_builds_assets_instead_of_crashing():
    assets = app.build_assets(["W"])   # a real CATA route we never tuned
    assert assets["W"]["bullet"].startswith(b"\x89PNG")
    assert assets["W"]["flash"].startswith(b"bicycle0")


# ------------------------------------------------------------ alert layer

def test_future_closure_classifies_planned_not_detour():
    assert app.classify_alert(
        "Starting Monday, Vairo Blvd will be closed for paving") == \
        "planned"
    assert app.classify_alert(
        "Stop 471 is temporarily closed, use stop 469") == "detour"
    assert app.classify_alert("V buses are on detour via Martin St") == \
        "detour"


def _bare_app(alerts):
    a = app.App.__new__(app.App)
    a.alerts = alerts
    return a


def test_other_alerts_reach_the_page_cycle():
    a = _bare_app([{"kind": "other", "type": "other",
                    "head": "Fare change takes effect", "period": "",
                    "routes": []}])
    key, mq, _c = a._pick_page()
    assert key == "alertpg"
    assert "FARE CHANGE" in mq


# ------------------------------------------------------------ flash policy

def _adopt_app(arrivals, index):
    a = app.App.__new__(app.App)
    a.arrivals = list(arrivals)
    a.index = index
    a.page_hold_until = 0.0
    a.flashed = []
    a.rendered = []

    async def _flash(route):
        a.flashed.append(route)

    async def _render():
        a.rendered.append(True)

    a.departure_flash = _flash
    a.render = _render
    return a


def _bus(mins, trip, route="V"):
    return (time.time() + mins * 60, route, trip, 0, True, False)


def test_soonest_bus_departure_flashes():
    a = _adopt_app([_bus(1, "t1"), _bus(9, "t2")], index=0)
    asyncio.run(a._adopt([_bus(9, "t2")]))
    assert a.flashed == ["V"]


def test_toured_bus_vanishing_goes_home_quietly():
    a = _adopt_app([_bus(1, "t1"), _bus(9, "t2")], index=1)
    asyncio.run(a._adopt([_bus(1, "t1")]))
    assert a.flashed == []
    assert a.index == 0
    assert a.rendered


def test_last_bus_crossing_flashes_into_empty_board():
    a = _adopt_app([_bus(1, "t1")], index=0)
    asyncio.run(a._adopt([]))
    assert a.flashed == ["V"]

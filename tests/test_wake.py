"""Press-to-wake tests: parse_wake, the is_awake gate (including the
stream-down degrade to always-on), and the input state machine — wake on
OK/START/dial from the OFF position only, extend on interaction, BACK
means lights out, and always-on mode keeps the old BACK-redraw."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "apps" / "catabus"))
import app  # noqa: E402


def test_parse_wake():
    assert app.parse_wake("15") == 900
    assert app.parse_wake("15m") == 900
    assert app.parse_wake("900s") == 900
    assert app.parse_wake("2m") == 120
    assert app.parse_wake("30s") == 60      # floor
    assert app.parse_wake("off") == 0
    assert app.parse_wake("0") == 0
    assert app.parse_wake("") == 0
    assert app.parse_wake("junk") is None


def wake_app(wake_secs=900, awake=False, switch_pos=app.SWITCH_OFF_POS):
    a = app.App.__new__(app.App)
    a.wake_secs = wake_secs
    a.input_stream_ok = True
    a.awake_until = time.time() + 300 if awake else 0.0
    a.switch_pos = switch_pos
    a.arrivals = [(time.time() + 300, "V", "t1", 0, True, False),
                  (time.time() + 900, "V", "t2", 0, True, False)]
    a.index = 0
    a.last_dial = 0.0
    a.rotate_hold_until = 0.0
    a.canvas_mode = "card"
    a.dot_count = 2
    a.raw_arrivals = list(a.arrivals)
    a.status_assets = {}
    a.alerts = []
    a.calls = []

    async def render():
        a.calls.append("render")

    async def slide_to(i, direction=1, user=True):
        a.calls.append(("slide", i))

    async def on_ok():
        a.calls.append("ok")

    async def on_start():
        a.calls.append("start")

    async def on_canvas_lost():
        a.calls.append("lost")

    async def go_dark():
        a.calls.append("dark")

    a.render = render
    a.slide_to = slide_to
    a.on_ok = on_ok
    a.on_start = on_start
    a.on_canvas_lost = on_canvas_lost
    a._go_dark = go_dark
    return a


def handle(a, *events):
    asyncio.run(a._handle_input_events(list(events)))


def test_is_awake_gates():
    a = wake_app(wake_secs=0)
    assert a.is_awake()                     # always-on
    a = wake_app(awake=False)
    a.input_stream_ok = False
    assert a.is_awake()                     # stream down + buses -> lit
    a.input_stream_ok = True
    assert not a.is_awake()                 # asleep
    a.awake_until = time.time() + 10
    assert a.is_awake()                     # inside the window


def test_overnight_stays_dark_even_degraded():
    # service done for the night: nothing on the ~99-minute horizon, so a
    # dead input stream must NOT light the quiet plate until morning
    a = wake_app(awake=False)
    a.input_stream_ok = False
    a.arrivals = []
    a.raw_arrivals = []
    assert not a.is_awake()
    # ~99 min before the first morning bus, scheduled arrivals reappear
    a.raw_arrivals = [(time.time() + 5400, "V", "t9", 0, False, False)]
    a.arrivals = list(a.raw_arrivals)
    assert a.is_awake()


def test_daytime_suspension_still_lights_degraded_board():
    a = wake_app(awake=False)
    a.input_stream_ok = False
    a.arrivals = []
    a.raw_arrivals = []
    a.status_assets = {"susp": {}}
    a.alerts = [{"kind": "suspension", "type": "suspension",
                 "head": "V not running", "period": "", "routes": ["V"]}]
    assert a.is_awake()                     # the NO BUSES story is worth showing


def test_ok_press_wakes_from_off_and_only_wakes():
    a = wake_app(awake=False)
    handle(a, ("button", app.BTN_OK, app.ACT_PRESS))
    assert a.awake_until > time.time()
    assert a.calls == ["render"]            # no alert page from the waker


def test_dial_turn_wakes_too():
    a = wake_app(awake=False)
    handle(a, ("encoder", 1, None))
    assert a.awake_until > time.time()
    assert a.calls == ["render"]            # the waking turn doesn't slide


def test_press_outside_off_position_is_ignored():
    a = wake_app(awake=False, switch_pos=4)  # settings
    handle(a, ("button", app.BTN_OK, app.ACT_PRESS))
    assert a.awake_until == 0.0
    assert a.calls == []


def test_unknown_position_still_wakes():
    a = wake_app(awake=False, switch_pos=None)
    handle(a, ("button", app.BTN_START, app.ACT_PRESS))
    assert a.awake_until > time.time()


def test_back_while_asleep_does_nothing():
    a = wake_app(awake=False)
    handle(a, ("button", app.BTN_BACK, app.ACT_RELEASE))
    assert a.awake_until == 0.0
    assert a.calls == []


def test_awake_buttons_do_their_jobs_and_extend():
    a = wake_app(awake=True)
    before = a.awake_until
    time.sleep(0.01)
    handle(a, ("button", app.BTN_OK, app.ACT_PRESS))
    assert a.calls == ["ok"]
    assert a.awake_until > before           # interaction extends


def test_awake_dial_slides():
    a = wake_app(awake=True)
    handle(a, ("encoder", 1, None))
    assert ("slide", 1) in a.calls


def test_back_while_awake_goes_dark():
    a = wake_app(awake=True)
    handle(a, ("button", app.BTN_BACK, app.ACT_RELEASE))
    assert a.awake_until == 0.0
    assert "dark" in a.calls


def test_switch_event_tracks_position():
    a = wake_app(awake=True)
    handle(a, ("switch", 4, None))
    assert a.switch_pos == 4
    assert "lost" in a.calls                # canvas died; redraw
    # and a wake press is now refused until the slider returns to OFF
    a.awake_until = 0.0
    a.calls.clear()
    handle(a, ("button", app.BTN_OK, app.ACT_PRESS))
    assert a.calls == []


def test_always_on_mode_keeps_back_redraw():
    a = wake_app(wake_secs=0)
    handle(a, ("button", app.BTN_BACK, app.ACT_RELEASE))
    assert a.calls == ["lost"]

"""Schedule/data-layer tests against tests/data/gtfs_trimmed.zip — a real
CATA GTFS snapshot (fall 2026, feed 2026-08-17..2026-12-13) trimmed by
tools/build_schedule.py. The asserted times were verified against the full
feed and the live realtime feed on 2026-08-26; regenerating the fixture
for a new semester will move them.
"""

import datetime
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "apps" / "catabus"))
import app  # noqa: E402

TZ = ZoneInfo("America/New_York")
FIXTURE = Path(__file__).resolve().parent / "data" / "gtfs_trimmed.zip"


def ts(*args):
    return datetime.datetime(*args, tzinfo=TZ).timestamp()


def hhmm(sched, epoch):
    return sched._local(epoch).strftime("%H:%M")


@pytest.fixture(scope="module")
def sched():
    s = app.Schedule(FIXTURE.read_bytes())
    app.register_routes(s.routes)
    return s


@pytest.fixture(scope="module")
def cfg(sched):
    return app.resolve_config(sched, "", "", "", "", "", "")


def by_route(sched, group, when, horizon=22 * 3600):
    out = {}
    for t, rid, trip, last in sched.departures(group, when, horizon):
        out.setdefault(app.designator(rid), []).append((t, trip, last))
    return out


def test_routes_and_colors(sched):
    assert {v[0] for v in sched.routes.values()} == {"V", "VE", "NV"}
    assert app.designator("43") == "V"
    assert app.designator("44") == "VE"
    assert app.designator("25") == "NV"
    assert app.line_color("V") == "#FF6600"


def test_default_config(cfg):
    prim, fb = cfg["groups"]
    assert prim["stops"] == ["504", "506"]
    assert prim["designators"] == ["V", "VE"]
    assert prim["dir_id"] == 1 and prim["dir_word"] == "campus"
    assert fb["stops"] == ["503", "507"]
    assert fb["designators"] == ["NV"] and fb["dir_id"] == 0


def test_gtfs_time_is_numeric():
    # "6:41" must sort before "10:25", and 24:19 is after midnight
    assert app.parse_gtfs_time("6:41:00") < app.parse_gtfs_time("10:25:00")
    assert app.parse_gtfs_time("24:19:00") == 24 * 3600 + 19 * 60


def test_weekday_spans(sched, cfg):
    # Wed 2026-08-26 (service 90): V 6:41-21:34, VE 7:24-19:11 at stop 504
    prim = cfg["groups"][0]
    deps = by_route(sched, prim, ts(2026, 8, 26, 4, 30))
    assert hhmm(sched, deps["V"][0][0]) == "06:41"
    assert hhmm(sched, deps["V"][-1][0]) == "21:34"
    assert len(deps["V"]) == 29
    assert hhmm(sched, deps["VE"][0][0]) == "07:24"
    assert hhmm(sched, deps["VE"][-1][0]) == "19:11"


def test_saturday_late_start_no_ve(sched, cfg):
    # Sat 2026-08-29 (service 58): V starts 8:17, VE rests
    prim = cfg["groups"][0]
    deps = by_route(sched, prim, ts(2026, 8, 29, 4, 30))
    assert hhmm(sched, deps["V"][0][0]) == "08:17"
    assert "VE" not in deps


def test_sunday_fallback(sched, cfg):
    # Sun 2026-08-30 (service 59): no V/VE at 504/506; NV covers Vairo
    # from the opposite-side stops 8:24-23:54
    prim, fb = cfg["groups"]
    when = ts(2026, 8, 30, 4, 30)
    assert sched.departures(prim, when, 22 * 3600) == []
    deps = by_route(sched, fb, when)
    assert hhmm(sched, deps["NV"][0][0]) == "08:24"
    assert hhmm(sched, deps["NV"][-1][0]) == "23:54"


def test_labor_day_runs_v_only(sched, cfg):
    # Mon 2026-09-07 (service 104): a full weekday-style V, no VE
    prim = cfg["groups"][0]
    deps = by_route(sched, prim, ts(2026, 9, 7, 4, 30))
    assert hhmm(sched, deps["V"][0][0]) == "06:41"
    assert "VE" not in deps


def test_service_day_rollover(sched):
    # 1am belongs to the previous service date (rollover at 3am)
    assert sched.service_date(ts(2026, 8, 27, 1, 0)) \
        == datetime.date(2026, 8, 26)
    assert sched.service_date(ts(2026, 8, 27, 4, 0)) \
        == datetime.date(2026, 8, 27)


def test_last_bus_flag(sched, cfg):
    prim = cfg["groups"][0]
    deps = sched.departures(prim, ts(2026, 8, 26, 20, 0), 3600 * 3)
    lasts = [(hhmm(sched, t), app.designator(r))
             for t, r, _trip, last in deps if last]
    assert ("21:34", "V") in lasts
    assert sched.last_departure(prim, ts(2026, 8, 26, 12, 0)) \
        == [t for t, *_ in deps][-1]


def test_next_departure_after_service(sched, cfg):
    # Sat 23:00: next V/VE is Monday 6:41; NV still has 23:24/23:54 runs
    prim, fb = cfg["groups"]
    t, rid = sched.next_departure(prim, ts(2026, 8, 29, 23, 0))
    assert sched._local(t).strftime("%a %H:%M") == "Mon 06:41"
    assert app.designator(rid) == "V"
    t, rid = sched.next_departure(fb, ts(2026, 8, 29, 23, 0))
    assert sched._local(t).strftime("%a %H:%M") == "Sat 23:24"


def test_fallback_blob_matches_fixture(sched):
    import base64
    blob = base64.b64decode(app.SCHEDULE_FALLBACK_B64)
    baked = app.Schedule(blob)
    assert {v[0] for v in baked.routes.values()} == {"V", "VE", "NV"}
    assert baked.feed_end == sched.feed_end


def test_direction_parse():
    assert app.parse_direction("campus") == (1, "campus")
    assert app.parse_direction("1") == (1, "campus")
    assert app.parse_direction("outbound") == (0, "outbound")
    with pytest.raises(SystemExit):
        app.parse_direction("sideways")


def test_bullet_art_generates():
    # every configured designator renders a bullet + flash anim; VE/NV
    # exercise the two-letter combine path
    assets = app.build_assets(["V", "VE", "NV"])
    for d in ("V", "VE", "NV"):
        assert assets[d]["bullet"].startswith(b"\x89PNG")
        assert assets[d]["flash"].startswith(b"bicycle0")


def test_status_words_fit():
    # raises SystemExit if any word overflows the plate
    out = app.build_status_assets()
    assert set(out) == {"susp", "planned", "delayed", "alertpg", "quiet",
                        "detour", "wash"}

# busybar-catabus

Live **CATA bus departures** (State College, PA / Penn State) on a
[BUSY Bar](https://busy.app)'s 72x16 LED display. Sibling of
[busybar-nyc-subway](https://github.com/Pranav-Karra-3301/busybar-nyc-subway):
same display grammar and art pipeline, CATA-native data layer.

Default config watches the **campus-bound V (Vairo Boulevard) and VE
(Vairo Express)** at the Vairo Blvd/Oakwood Ave corner (stop 504) and
lower Vairo Village (stop 506). Any routes/stops work via env.

## What it shows

- **Next bus**: route plate (22x15 shaded rounded rectangle — CATA's
  two-letter routes need more room than a subway-style disk — letters
  from the Bar's own fonts), minutes in extra-large type, position dots for up to 8 upcoming
  buses. Dial scrolls through them (over USB or a forwarded status socket).
- **Only buses you can catch**: `WALK` maps each route to the minutes it
  takes to reach its stop (default `V:2,VE:4,NV:3`, matching the default
  stops). A bus departing sooner than its walk is hidden — and the moment
  the shown bus crosses that line, its departure flash plays: gone is
  gone, whether it left the stop or just left *you* behind.
- **Press-to-wake** (`WAKE=10m`): the board never lights without a
  gesture — press OK/START, turn the dial, or flick the slider out of
  OFF and back (the flick works with no forwarder at all: while the
  input stream is down the app polls `GET /input/switch` over the cloud,
  firmware api >= 27.7). It shows for the window after the last
  interaction, then goes dark; BACK turns it off early. Dark doubles as
  the away state: nobody home, nobody gestures, nothing shows — at any
  hour. Default `off` = always-on.
- **Idle auto-rotation**: with the dial untouched, the board tours the
  next few catchable buses (`ROTATE` seconds each, default 7, over the
  first `ROTATE_DEPTH`, default 3) and wraps back to the soonest. Any
  dial motion pauses the tour for 25s; `ROTATE=off` restores the
  static soonest-bus board.
- **Departure flash**: full-screen sweep in the route color when the shown
  bus leaves, compiled as a device-side 60fps `.anim`.
- **Realtime + schedule, merged by trip identity**: Avail's TripUpdates
  cover ~90 minutes out; the static GTFS pads the rest, so a 32-minute
  headway still fills the board. Scheduled (not-yet-dispatched) entries
  are marked in the log with `*`.
- **Service awareness**: the day's final departure carries a **LAST** tag;
  after the last bus a calm **NO BUSES / NEXT BUS V MONDAY 6:41A** plate
  takes over. On Sundays — V/VE don't run — the board falls back to the
  **NV** loop at the Vairo Village stops (503/507), which covers Vairo
  8:24a–11:54p.
- **Status plates** (busy-mode grammar, from the GTFS-realtime Alerts feed
  plus InfoPoint's per-route messages with their explicit detour flag):
  red **NO BUSES** suspension takeover (only when the board is empty —
  while buses run, a suspension-kind alert pages as **ALERT** instead),
  hazard-striped **PLANNED**, blue
  **DETOUR** page, an amber corner dot + periodic **ALERT** page cycle for
  everything else. A held or late bus (vehicle stopped >3 min against the
  feed's own clock, or running ≥4 min behind schedule) keeps the normal
  board — the live ETA (delay already baked in) in white, `min` and all,
  dial still scrolling — and a **shaded red plate sliding in under the
  minutes** is the whole signal.

## Data sources (all public, no API key)

| What | Where |
|---|---|
| Static GTFS | `https://catabus.com/wp-content/uploads/google_transit.zip` (downloaded at startup, cached beside the app, refreshed daily; a trimmed fallback for the default stops is baked into `app.py`) |
| TripUpdates / VehiclePositions / Alerts | `https://gtfs-rt.myavail.cloud/GtfsProtoBuf?FeedLabel=CATA-SC&FeedType=…` (~10s fresh) |
| InfoPoint REST (detour flags) | `https://realtime.catabus.com/InfoPoint/rest/Routes/GetVisibleRoutes` |

CATA publishes a new GTFS zip each semester (current feed runs through
2026-12-13); the daily re-download tracks that automatically. Re-run
`tools/build_schedule.py <zip> --write` occasionally to refresh the baked
fallback + test fixture.

## Running

```sh
cd apps/catabus
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                      # default Vairo config
.venv/bin/python app.py --list-stops vairo   # find stop ids
.venv/bin/python app.py --routes V --stops 504 --direction campus
.venv/bin/python app.py --demo               # departure-flash demo
.venv/bin/python app.py --demo-alerts        # every status screen
.venv/bin/python app.py --clear
```

**No Bar handy?** `--preview` composites every draw to 8x-scaled PNGs in
`./preview/` instead of talking to hardware — alone it renders one live
board frame; with `--demo`/`--demo-alerts` it captures the staged
sequences. (Preview approximates firmware text leading and shows marquees
static; punctuation outside the baked glyph tables renders blank.)

Config env vars are documented in `apps/catabus/app.py`'s docstring —
`ROUTES`, `STOPS`, `DIRECTION`, `FALLBACK_*`, `BUSYBAR_TARGET/PRIORITY/
APP_NAME/WS/ALERTS`, `BUSYBAR_CLOUD_TOKEN`, `CATA_GTFS_URL`, `CATA_RT_BASE`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install pytest requests websockets
.venv/bin/python -m pytest tests/            # schedule + art tests
.venv/bin/python tools/build_glyphs.py --write    # refonts GLYPHS block
.venv/bin/python tools/bullet_editor.py      # visual letter tuning
.venv/bin/python tools/build_schedule.py --write  # refresh baked schedule
```

`tools/dial_forward.py` forwards the Bar's USB status websocket over a
tailnet so the dial works when the app runs on a server (`BUSYBAR_WS`).

## Hardware verification checklist

Verified live 2026-09-01 (manager library install on a VPS, cloud relay,
forwarded dial):

- [ ] V / VE / NV route-plate legibility at LED scale; tune letters with
      `tools/bullet_editor.py` (arrow keys, sizes, Save bakes offsets)
- [x] departure flash `.anim` plays smoothly and ends black
- [x] status plates + amber wash page cycle; live DELAYED state caught a
      real held V within minutes of install
- [ ] LAST tag placement over "min"
- [x] dial scroll (forwarded WS) + idle behavior; auto-rotation tours the
      next catchable buses and wraps home
- [x] manager install as a library app (priority 30, `BUSYBAR_WS` set);
      walk filter live ("N past walk" in the log)

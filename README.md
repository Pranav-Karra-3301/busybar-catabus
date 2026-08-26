# busybar-catabus

Live **CATA bus departures** (State College, PA / Penn State) on a
[BUSY Bar](https://busy.app)'s 72x16 LED display. Sibling of
[busybar-nyc-subway](https://github.com/Pranav-Karra-3301/busybar-nyc-subway):
same display grammar and art pipeline, CATA-native data layer.

Default config watches the **campus-bound V (Vairo Boulevard) and VE
(Vairo Express)** at the Vairo Blvd/Oakwood Ave corner (stop 504) and
lower Vairo Village (stop 506). Any routes/stops work via env.

## What it shows

- **Next bus**: route bullet (shaded disk, letters from the Bar's own
  fonts), minutes in extra-large type, position dots for up to 8 upcoming
  buses. Dial scrolls through them (over USB or a forwarded status socket).
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
  red **NO BUSES** suspension takeover, hazard-striped **PLANNED**, red
  **DELAYED** when the shown bus is held (vehicle stopped >3 min against
  the feed's own clock) or running ≥4 min late, blue **DETOUR** page, an
  amber corner dot + periodic **ALERT** page cycle for everything else.

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

## Hardware verification checklist (pending — built off-Bar)

Everything below works in `--preview`; confirm on the device when it's
back in reach:

- [ ] V / VE / NV bullet legibility at LED scale; tune letters with
      `tools/bullet_editor.py` (arrow keys, sizes, Save bakes offsets)
- [ ] departure flash `.anim` plays smoothly and ends black
- [ ] status plates + amber wash page cycle; marquee pass timing
- [ ] LAST tag placement over "min"
- [ ] dial scroll + 25s idle reset (USB, then forwarded WS)
- [ ] 409 politeness at priority 1; manager install as a library app with
      a `vairo-campus` variation (priority 30, `BUSYBAR_WS` set)

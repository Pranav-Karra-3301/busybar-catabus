#!/usr/bin/env python3
"""Live CATA bus departures (State College, PA) on a BUSY Bar.

Watches Penn State CATA stops — by default the campus-bound V (Vairo
Boulevard) and VE (Vairo Express) at the Vairo Blvd/Oakwood Ave corner
(stop 504) and lower Vairo Village (stop 506). The 72x16 front display
shows the next bus as a route bullet (15x15 shaded disk, route letters
baked in), minutes to departure in extra-large type, and position dots
down the right edge. When the shown bus departs, a full-screen wipe in
the route's color sweeps through, then the next bus slides in.

Data comes from CATA's public feeds (no API key): the static GTFS zip
(schedule, downloaded at startup and cached) plus Avail's GTFS-realtime
TripUpdates / VehiclePositions / Alerts. Realtime predictions and the
schedule merge by trip identity, so the board stays populated beyond the
realtime horizon and knows when service ends: after the last bus it shows
a quiet NO BUSES plate with the next departure ("V MON 6:41A"), and on
Sundays — when V/VE do not run — it falls back to the NV loop at the
Vairo Village stops so the board is useful every day.

Route bullets and departure animations are GENERATED at startup for the
configured routes (colors from routes.txt, glyphs from the BUSY Bar's own
fonts) and uploaded once; the departure flash is a compiled .anim the
device plays at 60fps.

Configuration (env vars, or the mirrored CLI flags which take precedence —
in busybar-manager, put these in a variation's "Environment variables"):

    ROUTES      route short names, default "V,VE"
    STOPS       GTFS stop ids, default "504,506"
    DIRECTION   campus | inbound | 1  /  outbound | 0   (default campus)
    FALLBACK_ROUTES / FALLBACK_STOPS / FALLBACK_DIRECTION
                the no-service fallback group, default NV @ 503,507
                outbound (the NV loop's Sunday Vairo coverage). Set
                FALLBACK_ROUTES=off to disable.

    CATA_GTFS_URL   static GTFS zip override
    CATA_RT_BASE    GTFS-realtime base override (…&FeedType= is appended)

    BUSYBAR_TARGET       auto | usb | wifi | cloud   (default auto)
    BUSYBAR_CLOUD_TOKEN  API token for the cloud target (cloud.busy.app)
    BUSYBAR_WIFI_URL     default http://172.16.105.41
    BUSYBAR_WIFI_TOKEN   default 8888
    BUSYBAR_PRIORITY     draw priority, default 1 (visible when the Bar's
                         switch is on OFF; set 30+ to show over the clock)
    BUSYBAR_APP_NAME     application_name override, default "catabus"
    BUSYBAR_WS           dial stream override: a ws:// URI for the Bar's
                         status socket. The Bar only serves it on USB, so
                         when the app runs elsewhere (busybar-manager, a
                         VPS, the cloud target) forward the USB port over
                         your tailnet/VPN — see tools/dial_forward.py —
                         and point this at it, e.g.
                         ws://100.x.y.z:8760/api/status/ws

Usage:
    python app.py                        # run forever
    python app.py --routes V,VE --stops 504,506 --direction campus
    python app.py --list-stops vairo     # find stop ids and served routes
    python app.py --demo                 # fake-data departure demo
    python app.py --demo-alerts          # stage every status screen
    python app.py --clear                # clear the display and exit
    python app.py --preview [--demo...]  # no Bar needed: render every
                                         # screen to PNGs in ./preview/

Dial: over USB the Bar's dial scrolls through upcoming arrivals (needs the
optional `websockets` package); other transports show the next bus unless
BUSYBAR_WS points them at a forwarded copy of the USB status socket.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import struct
import sys
import time
import zlib

requests = None  # imported lazily in main() — keeps `--help` stdlib-only for
#                  the busybar-manager options probe (it runs the system python)

USB_URL = "http://10.0.4.20"
WIFI_URL = os.environ.get("BUSYBAR_WIFI_URL", "http://172.16.105.41")
WIFI_TOKEN = os.environ.get("BUSYBAR_WIFI_TOKEN", "8888")
CLOUD_URL = "https://api.busy.app"
CLOUD_TOKEN = os.environ.get("BUSYBAR_CLOUD_TOKEN", "")
TARGET = os.environ.get("BUSYBAR_TARGET", "auto")
try:
    PRIORITY = int(os.environ.get("BUSYBAR_PRIORITY", "1"))
except ValueError:
    PRIORITY = 1
APP_NAME = os.environ.get("BUSYBAR_APP_NAME", "catabus")
WS_OVERRIDE = os.environ.get("BUSYBAR_WS", "")

WHITE = "#FFFFFFFF"

FETCH_SECS = 30          # realtime poll interval
SCHED_MERGE_MIN = 20     # pad with scheduled trips only this far out — a
#                          nearer trip the realtime feed doesn't know is
#                          more likely gone than merely undispatched
TICK_SECS = 2            # supervisor cadence for minute flips
BLOCKED_RETRY_SECS = 3   # draw-attempt cadence while another app owns screen
IDLE_RESET_SECS = 25     # snap back to the next train after dial inactivity
ELEMENT_TIMEOUT = 90     # stale elements self-erase if we stop pushing
MAX_ARRIVALS = 8         # 8 position dots x 2px = the 16px display height
FRAME_SECS = 0.04        # target animation frame interval (local transports)
SLIDE_OUT = (-1, -2, -4, -7, -11, -16)   # eased: accelerate off-screen
SLIDE_IN = (11, 7, 4, 2, 1, 0)           # eased: decelerate into place
# Departure flash: sweep-in, hold, fade-to-black — compiled to a .anim the
# device plays at 60fps, so it is smooth even over the cloud relay.
FLASH_FRAMES = (15, 84, 12)  # sweep, hold, fade — the single source of truth
FPS = 60
FLASH_ANIM_SECS = sum(FLASH_FRAMES) / FPS

# Service status (GTFS-realtime alerts, held/late buses, detours) rendered
# in the firmware's busy-mode plate grammar. BUSYBAR_ALERTS=off disables it.
ALERTS_ON = os.environ.get("BUSYBAR_ALERTS", "on").lower() not in (
    "off", "0", "no", "false")
ALERTS_POLL_SECS = 120
ALERT_PAGE_EVERY = 75     # seconds between alert-page interruptions
HELD_AFTER_SECS = 180     # STOPPED_AT with no movement this long = held
DELAY_PLATE_SECS = 240    # shown bus this late -> DELAYED takeover plate
WASH_SECS = 0.6           # the compiled amber wash that covers page swaps
MARQUEE_RATE = 1400       # px/min for the in-plate headline marquee
AMBER = "#FFB000FF"

# ------------------------------------------------------------------- routes

RT_BASE = os.environ.get(
    "CATA_RT_BASE",
    "https://gtfs-rt.myavail.cloud/GtfsProtoBuf?FeedLabel=CATA-SC")
GTFS_URL = os.environ.get(
    "CATA_GTFS_URL",
    "https://catabus.com/wp-content/uploads/google_transit.zip")


def rt_url(feed_type):
    return f"{RT_BASE}&FeedType={feed_type}"


# route short name -> (bullet color override, palette override). Colors
# default to routes.txt route_color; entries here win where the printed
# color doesn't survive LED scale (NV's 635f5f grey reads as mud). Every
# letter is white with the firmware's baked drop shadow (_stamp_letter).
DESIGNATOR_META = {
    "V": ("#FF6600", None),
    "VE": ("#FF9966", None),
    "NV": ("#8A8D8F", None),
}

# CATA has no rush-hour diamond convention; the machinery stays (the art
# pipeline and bullet_editor share it) but nothing maps to an express.
EXPRESS_OF = {}

PALETTES = {}

# populated from the loaded schedule: GTFS route_id -> short name ("43" ->
# "V") and short name -> route_color hex. Module-level because designator()
# is called from the decoders, which predate any config object.
ROUTE_SHORT = {}
ROUTE_COLOR = {}


def register_routes(routes):
    """routes: {route_id: (short, long_name, colorhex)} from the schedule."""
    for rid, (short, _long, color) in routes.items():
        ROUTE_SHORT[rid] = short
        if color:
            ROUTE_COLOR.setdefault(short, "#" + color.lstrip("#"))


def designator(route_id):
    """Collapse a GTFS route_id onto the bullet we draw for it."""
    r = route_id.upper()
    return ROUTE_SHORT.get(r, ROUTE_SHORT.get(route_id, r))


def base_desig(desig):
    return desig


def is_express(desig):
    return False


def line_color(desig):
    meta = DESIGNATOR_META.get(desig)
    if meta:
        return meta[0]
    return ROUTE_COLOR.get(desig, "#8A8D8F")


def letter_for(desig):
    return desig


# --- BEGIN GENERATED: SCHEDULE ---
# generated by tools/build_schedule.py — do not edit by hand. A trimmed
# GTFS zip (routes V/VE/NV at the default + fallback stops only) used when
# the live google_transit.zip can't be downloaded and no cache exists.
SCHEDULE_FALLBACK_B64 = (
    "UEsDBBQAAAAIAK98Gl1GwtzGkgAAAO0AAAAKAAAAYWdlbmN5LnR4dE2NwQ6CMBBE7yT8yUK1"
    "eiCeJN49efFE1rpAI902paTRr1c0Nb29yczL4ECsnp2+A/6I0VDixU8Jgzb0svyvJuQhsRuz"
    "okdPuUgG9VQWGzi1lxbGENxBiBhjrTDgbZlrZQ20hrxWKM4Uu6v1DyCGZruXu0bKRn6t+aNl"
    "SmKx/lXWBW15rjT31htcg4B1ccyUsngDUEsDBBQAAAAIAK98Gl2DZIKl8wAAACUCAAAKAAAA"
    "cm91dGVzLnR4dKWQwWqEMBCG74Lv0AcYG62r1D0W1mOPXpdsHFdBjczEoG/fRLS0eNwhkD9/"
    "Zv58hPRs8N7VIJ84qtUr2ixuNZn7KAfcjV6Pz7/nGlnt0qzT4c7U70rpXtPRgIv5Z7AP11Qj"
    "hcFHBjF8V35BCq0xE1+FUNLIx8zvSg+CkWynkCNWLdZzj3xcC17Z4BANchKjjVppkbjtCJ1D"
    "phsjKzvSkdGNIXQB2+u/MQLyNGuyBpqtIEnC4JI6mgoqP/f2pV2XlVS/RmZ3jMcRd+IoyzyP"
    "Y7f5guTTcVw8x20HuS3Tiwh4YsBlIuTzn5RlUeQ5xFtBUoTBD1BLAwQUAAAACACvfBpdjfVV"
    "LkAPAACCpwAACQAAAHRyaXBzLnR4dK2dS28cyQ3H7wHyHXzKSYL7Ua8+9hO5BMghyNXQY7IW"
    "VrEMSbbz8aNmT/2nPdOlLpI6LHbtnSqS9SvOzJ9sSs9PP14PXx7ur14Ozz8f7ug/X58fvuPf"
    "Xw839y8Pf3y7un94Pty9Pjx9m//X7ePT3Z+07OvNd1r06+vh8Hj39ebh+cvN3d3h5eXh9vFw"
    "dfvw5+Hly83j49Ovw/1f/1LZq7IwV6+hCde35XT98ujC1d8PD398fX35/I+b59eHb5///bbF"
    "0+d/Pf3n9flweLkqruryqvTe119evn5/+2Ox2qZz17dVmblNnd5m5c3w9Ovb69s/V+Vi2Dq3"
    "sWLqP8T/aTz5vzZcpww3ZfsRETdlz4y4ccVHRNy4ihtxKPJdtbhbw9uC2u16WjWzp6663GW+"
    "WmQ2Y5cl3s1dVr6snCez1trLBXSx1M7P9yo6f3FmG2bpWqmjXW4VI9rlUmmjpTvFitaPHxFt"
    "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"
)
# --- END GENERATED: SCHEDULE ---

# --- BEGIN GENERATED: GLYPHS ---
# generated by tools/build_glyphs.py — do not edit by hand
BULLET_GLYPHS = {
    "0": [".####.", "##..##", "##.###", "###.##", "##..##", "##..##", ".####."],
    "1": ["..##..", ".###..", "####..", "..##..", "..##..", "..##..", "######"],
    "2": [".####.", "##..##", "....##", "..###.", ".##...", "##....", "######"],
    "3": [".####.", "##..##", "....##", "..###.", "....##", "##..##", ".####."],
    "4": ["..###.", ".####.", ".#.##.", "##.##.", "##.##.", "######", "...##."],
    "5": ["######", "##....", "##....", "#####.", "....##", "....##", "#####."],
    "6": [".####.", "##..##", "##....", "#####.", "##..##", "##..##", ".####."],
    "7": ["######", "....##", "...##.", "..##..", "..##..", ".##...", ".##..."],
    "8": [".####.", "##..##", "##..##", ".####.", "##..##", "##..##", ".####."],
    "9": [".####.", "##..##", "##..##", ".#####", "....##", "##..##", ".####."],
    "A": ["..##..", "..##..", ".####.", ".#.##.", ".####.", "##..##", "##..##"],
    "B": ["#####.", "##..##", "##..##", "#####.", "##..##", "##..##", "#####."],
    "C": [".####.", "##..##", "##....", "##....", "##....", "##..##", ".####."],
    "D": ["####..", "##.##.", "##..##", "##..##", "##..##", "##.##.", "####.."],
    "E": ["#####", "##...", "##...", "####.", "##...", "##...", "#####"],
    "F": ["#####", "##...", "##...", "####.", "##...", "##...", "##..."],
    "G": [".####.", "##..##", "##....", "##.###", "##..##", "##..##", ".###.#"],
    "H": ["##..##", "##..##", "##..##", "######", "##..##", "##..##", "##..##"],
    "I": ["####", ".##.", ".##.", ".##.", ".##.", ".##.", "####"],
    "J": ["...##", "...##", "...##", "...##", "##.##", "##.##", ".###."],
    "K": ["##..##", "##.##.", "####..", "###...", "####..", "##.##.", "##..##"],
    "L": ["##...", "##...", "##...", "##...", "##...", "##...", "#####"],
    "M": ["##.....##", "###...###", "####.####", "#########", "##.###.##", "##..#..##", "##.....##"],
    "N": ["##...##", "###..##", "####.##", "#######", "##.####", "##..###", "##...##"],
    "O": [".####.", "##..##", "##..##", "##..##", "##..##", "##..##", ".####."],
    "P": ["#####.", "##..##", "##..##", "#####.", "##....", "##....", "##...."],
    "Q": [".####.", "##..##", "##..##", "##..##", "##.#.#", "##..#.", ".###.#"],
    "R": ["#####.", "##..##", "##..##", "#####.", "##..##", "##..##", "##..##"],
    "S": [".####.", "##..##", "##....", ".####.", "....##", "##..##", ".####."],
    "T": ["######", "..##..", "..##..", "..##..", "..##..", "..##..", "..##.."],
    "U": ["##..##", "##..##", "##..##", "##..##", "##..##", "##..##", ".####."],
    "V": ["##...##", "##...##", "##...##", ".##.##.", ".##.##.", "..###..", "..###.."],
    "W": ["##......##", "##..##..##", "##..##..##", "##.####.##", "##########", ".###..###.", ".##....##."],
    "X": ["##..##", "##..##", ".####.", "..##..", ".####.", "##..##", "##..##"],
    "Y": ["##..##", "##..##", ".####.", ".####.", "..##..", "..##..", "..##.."],
    "Z": ["######", "....##", "...##.", "..##..", ".##...", "##....", "######"],
}
XL_GLYPHS = {
    "0": [".#####.", "#######", "##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "#######", ".#####."],
    "1": [".##.", "###.", "###.", ".##.", ".##.", ".##.", ".##.", ".##.", "####", "####"],
    "2": [".#####.", "#######", "##...##", "....###", "...###.", "..###..", ".###...", "###....", "#######", "#######"],
    "3": [".#####.", "#######", "##...##", ".....##", "...###.", "...####", ".....##", "##...##", "#######", ".#####."],
    "4": ["..####.", ".#####.", ".##.##.", "###.##.", "##..##.", "##..##.", "##..##.", "#######", "#######", "....##."],
    "5": ["#######", "#######", "##.....", "##.....", "######.", "#######", ".....##", "##...##", "#######", ".#####."],
    "6": [".#####.", "#######", "##...##", "##.....", "######.", "#######", "##...##", "##...##", "#######", ".#####."],
    "7": ["#######", "#######", ".....##", "....##.", "...###.", "...##..", "..##...", "..##...", "..##...", "..##..."],
    "8": [".#####.", "#######", "##...##", "##...##", ".#####.", "#######", "##...##", "##...##", "#######", ".#####."],
    "9": [".#####.", "#######", "##...##", "##...##", "#######", ".######", ".....##", "##...##", "#######", ".#####."],
    "A": [".#####.", "#######", "##...##", "##...##", "#######", "#######", "##...##", "##...##", "##...##", "##...##"],
    "B": ["######.", "#######", "##...##", "##...##", "######.", "#######", "##...##", "##...##", "#######", "######."],
    "C": [".#####.", "#######", "##...##", "##.....", "##.....", "##.....", "##.....", "##...##", "#######", ".#####."],
    "D": ["#####..", "######.", "##..###", "##...##", "##...##", "##...##", "##...##", "##..###", "######.", "#####.."],
    "E": ["######", "######", "##....", "##....", "#####.", "#####.", "##....", "##....", "######", "######"],
    "F": ["######", "######", "##....", "##....", "#####.", "#####.", "##....", "##....", "##....", "##...."],
    "G": [".#####.", "#######", "##...##", "##.....", "##..###", "##..###", "##...##", "##...##", "#######", ".#####."],
    "H": ["##...##", "##...##", "##...##", "##...##", "#######", "#######", "##...##", "##...##", "##...##", "##...##"],
    "I": ["####", "####", ".##.", ".##.", ".##.", ".##.", ".##.", ".##.", "####", "####"],
    "J": ["....##", "....##", "....##", "....##", "....##", "....##", "##..##", "##..##", "######", ".####."],
    "K": ["##...##", "##...##", "##..###", "##.###.", "#####..", "######.", "##..###", "##...##", "##...##", "##...##"],
    "L": ["##....", "##....", "##....", "##....", "##....", "##....", "##....", "##....", "######", "######"],
    "M": ["##....##", "##....##", "###..###", "########", "########", "##.##.##", "##....##", "##....##", "##....##", "##....##"],
    "N": ["##...##", "##...##", "###..##", "####.##", "#######", "##.####", "##..###", "##...##", "##...##", "##...##"],
    "O": [".#####.", "#######", "##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "#######", ".#####."],
    "P": ["######.", "#######", "##...##", "##...##", "#######", "######.", "##.....", "##.....", "##.....", "##....."],
    "Q": [".#####.", "#######", "##...##", "##...##", "##...##", "##...##", "##.####", "##.###.", "#######", ".###.##"],
    "R": ["######.", "#######", "##...##", "##...##", "#######", "######.", "##...##", "##...##", "##...##", "##...##"],
    "S": [".#####.", "#######", "##...##", "##.....", "######.", ".######", ".....##", "##...##", "#######", ".#####."],
    "T": ["######", "######", "..##..", "..##..", "..##..", "..##..", "..##..", "..##..", "..##..", "..##.."],
    "U": ["##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "#######", ".#####."],
    "V": ["##....##", "##....##", "##....##", "###..##.", ".##..##.", ".##..##.", ".##..##.", ".##..##.", "..####..", "..####.."],
    "W": ["##....##", "##....##", "##....##", "##.##.##", "##.##.##", "##.##.##", "########", "########", ".##..##.", ".##..##."],
    "X": ["##...##", "##...##", "##...##", "###.###", ".#####.", ".#####.", "###.###", "##...##", "##...##", "##...##"],
    "Y": ["##...##", "##...##", "##...##", "##...##", "#######", ".######", ".....##", "##...##", "#######", ".#####."],
    "Z": ["######", "######", "....##", "...###", "..###.", ".###..", "###...", "##....", "######", "######"],
}
TINY_GLYPHS = {
    "0": [".#.", "#.#", "#.#", ".#."],
    "1": [".#.", "##.", ".#.", "###"],
    "2": ["##.", "..#", "#..", "###"],
    "3": ["##.", ".##", "..#", "##."],
    "4": ["..#", "#.#", "###", "..#"],
    "5": ["###", "#..", "..#", "##."],
    "6": [".##", "#..", "###", "###"],
    "7": ["###", "..#", ".#.", "#.."],
    "8": [".#.", "###", "#.#", ".#."],
    "9": [".##", "#.#", "###", "..#"],
    "A": [".#.", "#.#", "###", "#.#"],
    "B": ["##.", "###", "#.#", "##."],
    "C": [".##", "#..", "#..", ".##"],
    "D": ["##.", "#.#", "#.#", "##."],
    "E": ["###", "##.", "#..", "###"],
    "F": ["###", "#..", "##.", "#.."],
    "G": [".##", "#..", "#.#", ".##"],
    "H": ["#.#", "#.#", "###", "#.#"],
    "I": ["###", ".#.", ".#.", "###"],
    "J": ["..#", "..#", "#.#", ".#."],
    "K": ["#..#", "#.#.", "###.", "#..#"],
    "L": ["#..", "#..", "#..", "###"],
    "M": ["#...#", "##.##", "#.#.#", "#...#"],
    "N": ["#..#", "##.#", "#.##", "#..#"],
    "O": [".##.", "#..#", "#..#", ".##."],
    "P": ["##.", "#.#", "##.", "#.."],
    "Q": [".##.", "#..#", "#..#", ".##.", "..#."],
    "R": ["##.", "#.#", "##.", "#.#"],
    "S": [".##", "#..", "..#", "##."],
    "T": ["###", ".#.", ".#.", ".#."],
    "U": ["#..#", "#..#", "#..#", ".##."],
    "V": ["#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#.#.#", "#.#.#", ".#.#."],
    "X": ["#.#", ".#.", "#.#", "#.#"],
    "Y": ["#...#", ".#.#.", "..#..", "..#.."],
    "Z": ["####", "..#.", ".#..", "####"],
}
CONDENSED_GLYPHS = {
    "0": [".##.", "#..#", "#.##", "##.#", "#..#", "#..#", ".##."],
    "1": ["..#.", ".##.", "#.#.", "..#.", "..#.", "..#.", "####"],
    "2": [".##.", "#..#", "...#", "..#.", ".#..", "#...", "####"],
    "3": [".##.", "#..#", "...#", ".##.", "...#", "#..#", ".##."],
    "4": ["..##", ".#.#", ".#.#", "#..#", "#..#", "####", "...#"],
    "5": ["####", "#...", "#...", "###.", "...#", "...#", "###."],
    "6": [".##.", "#..#", "#...", "###.", "#..#", "#..#", ".##."],
    "7": ["####", "...#", "..#.", "..#.", ".#..", ".#..", ".#.."],
    "8": [".##.", "#..#", "#..#", ".##.", "#..#", "#..#", ".##."],
    "9": [".##.", "#..#", "#..#", ".###", "...#", "#..#", ".##."],
    "A": ["..#..", "..#..", ".#.#.", ".#.#.", ".###.", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["###..", "#..#.", "#...#", "#...#", "#...#", "#..#.", "###.."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": ["###", ".#.", ".#.", ".#.", ".#.", ".#.", "###"],
    "J": ["...#", "...#", "...#", "...#", "#..#", "#..#", ".##."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#...", "#...", "#...", "#...", "#...", "#...", "####"],
    "M": ["#.....#", "##...##", "##...##", "#.#.#.#", "#.#.#.#", "#..#..#", "#..#..#"],
    "N": ["#...#", "##..#", "##..#", "#.#.#", "#..##", "#..##", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "#...#"],
    "S": [".###.", "#...#", "#....", ".###.", "....#", "#...#", ".###."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", ".#.#.", ".#.#.", "..#..", "..#.."],
    "W": ["#.....#", "#..#..#", "#..#..#", "#.#.#.#", "#.#.#.#", ".#...#.", ".#...#."],
    "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", ".#.#.", "..#..", "..#..", "..#.."],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
}
SMALL_GLYPHS = {
    "0": [".#.", "#.#", "#.#", "#.#", ".#."],
    "1": [".#", "##", ".#", ".#", ".#"],
    "2": ["##.", "..#", ".#.", "#..", "###"],
    "3": ["##.", "..#", ".#.", "..#", "##."],
    "4": ["..#", ".##", "#.#", "###", "..#"],
    "5": ["###", "#..", "##.", "..#", "##."],
    "6": [".##", "#..", "##.", "#.#", ".#."],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": [".#.", "#.#", ".#.", "#.#", ".#."],
    "9": [".#.", "#.#", ".##", "..#", "##."],
    "A": [".##.", "#..#", "####", "#..#", "#..#"],
    "B": ["###.", "#..#", "###.", "#..#", "###."],
    "C": [".##.", "#..#", "#...", "#..#", ".##."],
    "D": ["###.", "#..#", "#..#", "#..#", "###."],
    "E": ["####", "#...", "###.", "#...", "####"],
    "F": ["####", "#...", "###.", "#...", "#..."],
    "G": [".###", "#...", "#.##", "#..#", ".###"],
    "H": ["#..#", "#..#", "####", "#..#", "#..#"],
    "I": ["#", "#", "#", "#", "#"],
    "J": ["..#", "..#", "..#", "#.#", ".#."],
    "K": ["#..#", "#.#.", "##..", "#.#.", "#..#"],
    "L": ["#..", "#..", "#..", "#..", "###"],
    "M": ["#...#", "##.##", "#.#.#", "#...#", "#...#"],
    "N": ["#..#", "##.#", "#.##", "#..#", "#..#"],
    "O": [".##.", "#..#", "#..#", "#..#", ".##."],
    "P": ["###.", "#..#", "###.", "#...", "#..."],
    "Q": [".##.", "#..#", "#..#", "#.#.", ".#.#"],
    "R": ["###.", "#..#", "###.", "#..#", "#..#"],
    "S": [".###", "#...", ".##.", "...#", "###."],
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "U": ["#..#", "#..#", "#..#", "#..#", ".##."],
    "V": ["#.#", "#.#", "#.#", ".#.", ".#."],
    "W": ["#.#.#", "#.#.#", "#.#.#", ".#.#.", ".#.#."],
    "X": ["#.#", "#.#", ".#.", "#.#", "#.#"],
    "Y": ["#.#", "#.#", ".#.", ".#.", ".#."],
    "Z": ["###", "..#", ".#.", "#..", "###"],
}
DISK_MASK = [".....#####.....", "...#########...", "..###########..", ".#############.", ".#############.", "###############", "###############", "###############", "###############", "###############", ".#############.", ".#############.", "..###########..", "...#########...", ".....#####....."]
BULLET_GLYPH_OVERRIDES = {
    "G": [".#####..", "##...##.", "##......", "##......", "##...###", "##....##", "##...###", ".#####.."],
    "Q": [".#####.", "##...##", "##...##", "##...##", "##...##", "##..###", ".#####.", ".....##"],
}
# --- END GENERATED: GLYPHS ---

# --- BEGIN GENERATED: OFFSETS ---
# per-icon letter tuning, edited with tools/bullet_editor.py.
# LETTER_OFFSETS: (dx, dy) nudges from dead center — the Q/G seeds carry
# the legacy hand alignment. LETTER_SIZES: glyph size override per icon
# ("tiny" ~3x4 / "bold" 7px / "xl" 10px); defaults are bullet=bold,
# flash=xl for locals and bold inside the express diamond mark.
LETTER_OFFSETS = {
    "bullet": {},
    "flash": {},
}
LETTER_SIZES = {
    "bullet": {
        "V": "xl",
    },
    "flash": {},
}
# --- END GENERATED: OFFSETS ---


# ----------------------------------------------------------------- schedule

class ConfigError(SystemExit):
    def __init__(self, message, display_hint):
        super().__init__(message)
        self.message = message
        self.display_hint = display_hint  # short string for the 72x16 screen


# GTFS times run past 24:00 (a 12:19am trip is "24:19" on the previous
# service date), so the service day flips well after the last owl trip.
SERVICE_ROLLOVER_SECS = 3 * 3600


def parse_gtfs_time(text):
    """"6:41:00" / "24:19:00" -> seconds after the service day's noon-12h
    anchor. Numeric, never lexical — GTFS hours are unpadded and exceed 24."""
    h, m, s = text.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


class Schedule:
    """The static GTFS timetable, reduced to what the board needs: which
    buses call at which stops when, and on which dates each service runs.
    CATA's feed has no calendar.txt — service is entirely explicit
    calendar_dates rows — but both forms are read for robustness."""

    def __init__(self, zip_bytes):
        import csv as _csv
        import io
        import zipfile

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))

        def rows(name):
            try:
                info = zf.getinfo(name)
            except KeyError:
                return
            with zf.open(info) as f:
                text = io.TextIOWrapper(f, "utf-8-sig", newline="")
                yield from _csv.DictReader(text)

        self.tz_name = "America/New_York"
        for r in rows("agency.txt"):
            self.tz_name = r.get("agency_timezone") or self.tz_name
            break
        try:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(self.tz_name)
        except Exception:
            self.tz = None  # fall back to local time (device tz matches)

        self.routes = {}       # route_id -> (short, long_name, color)
        self.short_to_id = {}
        for r in rows("routes.txt"):
            short = (r.get("route_short_name") or r["route_id"]).upper()
            self.routes[r["route_id"]] = (
                short, r.get("route_long_name") or "",
                r.get("route_color") or "")
            self.short_to_id[short] = r["route_id"]

        self.stops = {}        # stop_id -> name
        for r in rows("stops.txt"):
            self.stops[r["stop_id"]] = r.get("stop_name") or r["stop_id"]

        self.cal = {}          # service_id -> set("YYYYMMDD")
        for r in rows("calendar.txt"):
            days = ("monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday", "sunday")
            import datetime as _dt
            start = _dt.datetime.strptime(r["start_date"], "%Y%m%d").date()
            end = _dt.datetime.strptime(r["end_date"], "%Y%m%d").date()
            active = self.cal.setdefault(r["service_id"], set())
            d = start
            while d <= end:
                if r.get(days[d.weekday()]) == "1":
                    active.add(d.strftime("%Y%m%d"))
                d += _dt.timedelta(days=1)
        for r in rows("calendar_dates.txt"):
            active = self.cal.setdefault(r["service_id"], set())
            if r["exception_type"] == "1":
                active.add(r["date"])
            else:
                active.discard(r["date"])
        self.feed_end = max((max(v) for v in self.cal.values() if v),
                            default="")

        self.trips = {}        # trip_id -> (route_id, service_id, dir_id)
        for r in rows("trips.txt"):
            self.trips[r["trip_id"]] = (
                r["route_id"], r["service_id"],
                int(r.get("direction_id") or 0))

        self.deps = {}         # stop_id -> [(secs, route_id, trip_id,
        #                                     dir_id, service_id)] sorted
        for r in rows("stop_times.txt"):
            trip = self.trips.get(r["trip_id"])
            if not trip or r.get("pickup_type") == "1":
                continue
            t = r.get("departure_time") or r.get("arrival_time")
            if not t:
                continue
            self.deps.setdefault(r["stop_id"], []).append(
                (parse_gtfs_time(t), trip[0], r["trip_id"],
                 trip[2], trip[1]))
        for lst in self.deps.values():
            lst.sort()

    # -- time plumbing -----------------------------------------------------

    def _local(self, epoch):
        import datetime as _dt
        if self.tz:
            return _dt.datetime.fromtimestamp(epoch, self.tz)
        return _dt.datetime.fromtimestamp(epoch)

    def service_date(self, epoch):
        """The GTFS service date active at `epoch` (flips at 3am local)."""
        return self._local(epoch - SERVICE_ROLLOVER_SECS).date()

    def _noon_anchor(self, date):
        """GTFS times count from noon-minus-12h so a DST change night
        doesn't shift every timetable entry."""
        import datetime as _dt
        noon = _dt.datetime.combine(date, _dt.time(12), tzinfo=self.tz) \
            if self.tz else _dt.datetime.combine(date, _dt.time(12))
        return noon.timestamp() - 12 * 3600

    def services_on(self, date):
        key = date.strftime("%Y%m%d")
        return {sid for sid, dates in self.cal.items() if key in dates}

    # -- queries -----------------------------------------------------------

    def _group_deps_on(self, group, date):
        """All of a group's scheduled departures for one service date, one
        entry per trip at its earliest watched stop: [(epoch, route_id,
        trip_id)] sorted."""
        active = self.services_on(date)
        if not active:
            return []
        base = self._noon_anchor(date)
        want_routes = set(group["route_ids"])
        per_trip = {}
        for stop in group["stops"]:
            for secs, rid, trip_id, dir_id, svc in self.deps.get(stop, []):
                if (svc not in active or rid not in want_routes
                        or dir_id != group["dir_id"]):
                    continue
                t = base + secs
                if trip_id not in per_trip or t < per_trip[trip_id][0]:
                    per_trip[trip_id] = (t, rid, trip_id)
        return sorted(per_trip.values())

    def departures(self, group, now, horizon_secs=None):
        """Upcoming scheduled departures for a group, spanning the service
        day boundary: [(epoch, route_id, trip_id, is_last)]. `is_last`
        marks each service date's final departure — the last bus."""
        import datetime as _dt
        today = self.service_date(now)
        out = []
        for date in (today, today + _dt.timedelta(days=1)):
            deps = self._group_deps_on(group, date)
            for i, (t, rid, trip_id) in enumerate(deps):
                if t < now:
                    continue
                if horizon_secs and t > now + horizon_secs:
                    break
                out.append((t, rid, trip_id, i == len(deps) - 1))
        return out

    def last_departure(self, group, now):
        """Epoch of the current service date's final departure, or None
        when the group has no service today."""
        deps = self._group_deps_on(group, self.service_date(now))
        return deps[-1][0] if deps else None

    def next_departure(self, group, after, days=14):
        """The first scheduled departure strictly after `after`, scanning
        up to `days` service dates: (epoch, route_id) or None."""
        import datetime as _dt
        start = self.service_date(after)
        for i in range(days):
            for t, rid, _trip in self._group_deps_on(
                    group, start + _dt.timedelta(days=i)):
                if t > after:
                    return t, rid
        return None


def parse_direction(value):
    v = (value or "").strip().lower()
    if v in ("", "campus", "inbound", "in", "i", "1"):
        return 1, "campus"
    if v in ("outbound", "out", "o", "0"):
        return 0, "outbound"
    raise ConfigError(
        f"DIRECTION {value!r} not understood — use campus/inbound/1 or "
        "outbound/0", "check DIRECTION")


def _make_group(schedule, routes_csv, stops_csv, direction, which):
    dir_id, dir_word = parse_direction(direction)
    shorts = [r.strip().upper() for r in routes_csv.split(",") if r.strip()]
    route_ids = []
    for s in shorts:
        rid = schedule.short_to_id.get(s)
        if not rid:
            raise ConfigError(
                f"{which} route {s!r} not in the GTFS feed — known: "
                + ", ".join(sorted(schedule.short_to_id)),
                f"check {which} ROUTES")
        route_ids.append(rid)
    stops = [s.strip() for s in stops_csv.split(",") if s.strip()]
    unknown = [s for s in stops if s not in schedule.stops]
    if unknown:
        raise ConfigError(
            f"{which} stop id(s) {unknown} not in the GTFS feed — try "
            "--list-stops", f"check {which} STOPS")
    served = {rid for s in stops
              for _t, rid, _tr, d, _svc in schedule.deps.get(s, [])
              if d == dir_id}
    missed = [ROUTE_SHORT.get(r, r) for r in route_ids if r not in served]
    if missed:
        print(f"warning: {which} route(s) {','.join(missed)} never call "
              f"{dir_word}-bound at stops {','.join(stops)}",
              file=sys.stderr)
    return {
        "stops": stops,
        "route_ids": route_ids,
        "designators": shorts,
        "dir_id": dir_id,
        "dir_word": dir_word,
        "label": schedule.stops.get(stops[0], stops[0]) if stops else which,
    }


def resolve_config(schedule, routes_csv, stops_csv, direction,
                   fb_routes, fb_stops, fb_direction):
    """-> {"groups": [primary(, fallback)], "label": str}. The fallback
    group covers days the primary routes don't run (Sundays: NV serves
    Vairo from the opposite-side stops while V/VE rest)."""
    register_routes(schedule.routes)
    primary = _make_group(schedule, routes_csv or "V,VE",
                          stops_csv or "504,506", direction, "primary")
    groups = [primary]
    if (fb_routes or "").lower() not in ("off", "none", "0"):
        groups.append(_make_group(
            schedule, fb_routes or "NV", fb_stops or "503,507",
            fb_direction or "outbound", "fallback"))
    return {"groups": groups, "label": primary["label"]}


def list_stops(schedule, query):
    register_routes(schedule.routes)
    q = (query or "").lower()
    hits = []
    for sid, name in schedule.stops.items():
        if q and q not in name.lower() and q != sid:
            continue
        served = {}
        for _t, rid, _tr, d, _svc in schedule.deps.get(sid, []):
            served.setdefault(designator(rid), set()).add(d)
        routes = " ".join(
            f"{r}({'/'.join('io'[x] for x in sorted(ds))})"
            for r, ds in sorted(served.items()))
        hits.append((name, sid, routes))
    for name, sid, routes in sorted(hits):
        print(f"{sid:>6}  {name:<44} {routes}")
    print(f"{len(hits)} stop(s)   (i = inbound/campus dir 1, o = outbound)")


def load_schedule():
    """Download the GTFS zip (cached beside the app, refreshed daily);
    fall back to the cache however stale, then to the baked blob."""
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "gtfs_cache.zip")
    data = None
    fresh = (os.path.exists(cache)
             and time.time() - os.path.getmtime(cache) < 86400)
    if not fresh:
        try:
            r = requests.get(GTFS_URL, timeout=60)
            r.raise_for_status()
            data = r.content
            tmp = cache + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, cache)
        except (requests.RequestException, OSError) as e:
            print(f"GTFS download failed ({e}); trying cache",
                  file=sys.stderr)
    if data is None and os.path.exists(cache):
        with open(cache, "rb") as f:
            data = f.read()
    if data is None and SCHEDULE_FALLBACK_B64:
        print("using the baked fallback schedule (default stops only)",
              file=sys.stderr)
        data = base64.b64decode(SCHEDULE_FALLBACK_B64)
    if data is None:
        raise ConfigError("no GTFS schedule: download failed, no cache, "
                          "no baked fallback", "no schedule")
    sched = Schedule(data)
    import datetime as _dt
    if sched.feed_end and \
            _dt.date.today().strftime("%Y%m%d") > sched.feed_end:
        print(f"warning: GTFS feed expired {sched.feed_end} — CATA "
              "publishes a new zip each semester", file=sys.stderr)
    return sched


# ------------------------------------------------------- art (pure stdlib)

def _hex_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _scale(c, k):
    return tuple(min(255, round(v * k)) for v in c)


def palette_for(desig):
    hexc, key = DESIGNATOR_META[base_desig(desig)]
    if key:
        return PALETTES[key]
    base = _hex_rgb(hexc)
    return {
        "spec": tuple(round(v + (255 - v) * 0.60) for v in base),
        "top": _scale(base, 1.15),
        "bot": _scale(base, 0.47),
        "lift": _scale(base, 0.62),
        "bullet_bot": _scale(base, 0.65),
    }


def png_encode(w, h, rows):
    """Minimal RGBA PNG writer. rows: list of rows of (r, g, b, a)."""
    raw = b"".join(
        b"\x00" + bytes(v for px in row for v in px) for row in rows)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + \
            struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# The firmware's glyph drop-shadow ratios, strongest first where they
# overlap — the same treatment the stock assets give the BUSY wordmark
# (hardware-verified on the truvo cards): each shadow pixel multiplies
# whatever it lands on.
GLYPH_SHADOW = ((0, 1, 0.43), (-1, 0, 0.78), (1, 0, 0.78), (0, 2, 0.76))

# 15x15 rotated square for the rush-hour express diamonds
DIAMOND_MASK = ["".join("#" if abs(x - 7) + abs(y - 7) <= 7 else "."
                        for x in range(15)) for y in range(15)]


SIZE_TABLES = {"tiny": TINY_GLYPHS, "small": SMALL_GLYPHS,
               "bold": BULLET_GLYPHS, "xl": XL_GLYPHS}


def _combine_glyphs(text, table, gap=1):
    """Stamp several glyphs side by side into one glyph grid — CATA route
    designators run to two letters (VE, NV), unlike subway bullets."""
    glyphs = [table[c] for c in text]
    h = max(len(g) for g in glyphs)
    rows = []
    for y in range(h):
        rows.append(("." * gap).join(
            g[y] if y < len(g) else "." * len(g[0]) for g in glyphs))
    return rows


def bullet_glyph(desig):
    ch = letter_for(desig)
    if len(ch) == 1:
        return BULLET_GLYPH_OVERRIDES.get(ch, BULLET_GLYPHS[ch])
    return _combine_glyphs(ch, SMALL_GLYPHS)


def letter_offset(kind, desig):
    return LETTER_OFFSETS.get(kind, {}).get(desig, (0, 0))


def default_size(kind, desig):
    if kind == "flash" and not is_express(desig):
        return "xl"
    if len(letter_for(desig)) > 1:
        return "small"  # two small letters fit the 15px disk; bold doesn't
    return "bold"


def glyph_for(kind, desig):
    """The glyph at this icon's tuned size; hand-tuned letterforms apply at
    the default bold size, and a size the font can't do falls back."""
    size = LETTER_SIZES.get(kind, {}).get(desig, default_size(kind, desig))
    ch = letter_for(desig)
    if len(ch) > 1:
        table = SIZE_TABLES.get(size, SMALL_GLYPHS)
        try:
            return _combine_glyphs(ch, table)
        except KeyError:
            return _combine_glyphs(ch, SMALL_GLYPHS)
    if size == "tiny":
        table = TINY_GLYPHS
    elif size == "xl":
        table = XL_GLYPHS
    else:
        return bullet_glyph(desig)
    return table.get(ch) or bullet_glyph(desig)


def _stamp_letter(grid, glyph, x0, y0, inside, extra_ink=()):
    """Bake a glyph in white with the firmware shadow ratios onto a mutable
    pixel grid (rows of RGB or RGBA tuples). `inside(x, y)` bounds both ink
    and shadow, so nothing bleeds off a disk or card."""
    ink = {(x0 + gx, y0 + gy)
           for gy, grow in enumerate(glyph)
           for gx, ch in enumerate(grow) if ch == "#"}
    ink |= set(extra_ink)
    ink = {p for p in ink if inside(*p)}
    shaded = set()
    for dx, dy, factor in GLYPH_SHADOW:
        for x, y in ink:
            tx, ty = x + dx, y + dy
            if (tx, ty) in ink or (tx, ty) in shaded or not inside(tx, ty):
                continue
            c = grid[ty][tx]
            grid[ty][tx] = tuple(round(v * factor) for v in c[:3]) + c[3:]
            shaded.add((tx, ty))
    for x, y in ink:
        grid[y][x] = (255, 255, 255) + grid[y][x][3:]


def make_bullet(desig):
    """15x15 shaded route disk (diamond for expresses) with the letter baked
    in white over the firmware drop shadow."""
    pal = palette_for(desig)
    express = is_express(desig)
    mask = DIAMOND_MASK if express else DISK_MASK
    size = len(mask)
    filled = [y for y in range(size) if "#" in mask[y]]
    top, bot = min(filled), max(filled)
    px = [[(0, 0, 0, 0)] * size for _ in range(size)]
    for y in range(size):
        for x in range(size):
            if mask[y][x] != "#":
                continue
            if express:
                # rim light down both upper edges (the diamond's "arc")
                spec = y <= size // 2 and (y == top or mask[y - 1][x] != "#")
            else:
                spec = y == top or (y == top + 1 and mask[top][x] != "#")
            if spec:
                c = pal["spec"]  # 1px specular arc following the rim
            else:
                c = _lerp(pal["top"], pal["bullet_bot"],
                          (y - top) / max(bot - top, 1))
            px[y][x] = (*c, 255)
    glyph = glyph_for("bullet", desig)
    gw, gh = len(glyph[0]), len(glyph)
    dx, dy = letter_offset("bullet", desig)
    _stamp_letter(px, glyph, (size - gw) // 2 + dx, (size - gh) // 2 + dy,
                  lambda x, y: 0 <= x < size and 0 <= y < size
                  and mask[y][x] == "#")
    return png_encode(size, size, px)


_LEGACY_BLACK = {"G", "N", "Q", "R", "W"}  # parity_check only: the old look


def flash_card(desig, legacy=False):
    """72x16 shaded field in the line color with the letter riding it — the
    XL letter for locals, the diamond-outline mark for expresses. Returns
    rows of (r, g, b). `legacy` reproduces the retired black-letter/no-shadow
    rendering so parity_check can still prove the pipeline byte-exact."""
    pal = palette_for(desig)
    w, h, r = 72, 16, 5
    rows = []
    for y in range(h):
        if y == 0:
            base = pal["spec"]
        elif y == h - 1:
            base = pal["lift"]
        else:
            base = _lerp(pal["top"], pal["bot"], (y - 1) / (h - 2))
        row = []
        for x in range(w):
            cx, cy = min(x, w - 1 - x), min(y, h - 1 - y)
            if cx < r and cy < r and (r - cx) ** 2 + (r - cy) ** 2 > r * r:
                row.append((0, 0, 0))  # rounded-off corner
                continue
            edge = min(cx, cy)
            scale = (0.25, 0.5, 0.75)[edge] if edge < 3 else 1.0
            row.append(tuple(round(c * scale) for c in base))
        rows.append(row)

    in_card = lambda x, y: 0 <= x < w and 0 <= y < h  # noqa: E731

    if is_express(desig):
        # the countdown-clock express mark: the bullet's diamond outline
        # with the small letter inside, centered on the field
        ox, oy = (w - 15) // 2, 0
        outline = {
            (ox + x, oy + y)
            for y in range(15) for x in range(15)
            if DIAMOND_MASK[y][x] == "#"
            and any(not (0 <= y + ey < 15 and 0 <= x + ex < 15
                         and DIAMOND_MASK[y + ey][x + ex] == "#")
                    for ex, ey in ((1, 0), (-1, 0), (0, 1), (0, -1)))}
        glyph = glyph_for("flash", desig)
        gw, gh = len(glyph[0]), len(glyph)
        dx, dy = letter_offset("flash", desig)
        _stamp_letter(rows, glyph, ox + (15 - gw) // 2 + dx,
                      oy + (15 - gh) // 2 + dy, in_card, extra_ink=outline)
        return rows

    glyph = (XL_GLYPHS[letter_for(desig)] if legacy
             else glyph_for("flash", desig))
    gw, gh = len(glyph[0]), len(glyph)
    dx, dy = letter_offset("flash", desig)
    x0, y0 = (w - gw) // 2 + dx, (h - gh) // 2 + dy
    if legacy:
        ink = (0, 0, 0) if desig in _LEGACY_BLACK else (255, 255, 255)
        for gy, grow in enumerate(glyph):
            for gx, ch in enumerate(grow):
                if ch == "#":
                    rows[y0 + gy][x0 + gx] = ink
        return rows
    _stamp_letter(rows, glyph, x0, y0, in_card)
    return rows


def _rgb_bytes(rows):
    return b"".join(bytes(v for px in row for v in px) for row in rows)


def flash_anim_frames(desig, legacy=False):
    """Sweep-in (eased), hold, fade-to-black — raw RGB frames."""
    card = flash_card(desig, legacy=legacy)
    w = len(card[0])
    black_row = [(0, 0, 0)] * w
    sweep, hold, fade = FLASH_FRAMES
    frames = []
    for i in range(sweep):  # ease-out cubic from x=-72 to 0
        t = (i + 1) / sweep
        x = round(-w * (1 - t) ** 3)
        frames.append(_rgb_bytes(
            [row[-x:] + black_row[: -x] if x else row for row in card]))
    frames += [_rgb_bytes(card)] * hold
    for i in range(fade):
        s = 1.0 - (i + 1) / fade
        frames.append(_rgb_bytes(
            [[tuple(round(v * s) for v in px) for px in row] for row in card]))
    return frames


# bicycle0 .anim container — byte-compatible port of the firmware's
# seq2anim.py (same as ~/busybar/tools/build_anim.py, minus PIL)

_MAX_BLOCKS = 127
_RLE_THRESHOLD = 3
_HEADER_FORMAT = "<8s BBBB BHB II III"
_SECTION_FORMAT = "<IIIB"
_FRAME_FORMAT = "<BBH"


def _rle_compress(source, blk_size):
    src_i, src_len = 0, len(source)
    dest = bytearray()
    while src_i < src_len:
        repeat_count = 0
        for i in range(src_i, src_len, blk_size):
            if source[i:i + blk_size] == source[src_i:src_i + blk_size]:
                repeat_count += 1
            else:
                break
        repeat_count = min(repeat_count, _MAX_BLOCKS)
        if repeat_count == 0:
            break
        if repeat_count < _RLE_THRESHOLD:
            repeat_count = 0
            verbatim_count = 0
            for i in range(src_i, src_len, blk_size):
                if source[i:i + blk_size] == source[i + blk_size:i + blk_size * 2]:
                    repeat_count += 1
                    if repeat_count > _RLE_THRESHOLD:
                        break
                else:
                    verbatim_count += 1 + repeat_count
                    repeat_count = 0
            verbatim_count += repeat_count
            verbatim_count = min(verbatim_count, _MAX_BLOCKS)
            dest.append(0x80 | verbatim_count)
            dest.extend(source[src_i:src_i + verbatim_count * blk_size])
            src_i += verbatim_count * blk_size
        else:
            dest.append(repeat_count)
            dest.extend(source[src_i:src_i + blk_size])
            src_i += repeat_count * blk_size
    return bytes(dest)


def anim_encode(frames, w, h, fps=FPS):
    """RGB frame bytes -> bicycle0 .anim blob (rgb888, stored BGR)."""
    encoded = []
    last = None
    for fb in frames:
        if fb == last:
            encoded[-1][1] += 1
            continue
        last = fb
        packed = bytearray()
        for i in range(0, len(fb), 3):
            packed.extend((fb[i + 2], fb[i + 1], fb[i]))
        packed = bytes(packed)
        rle = _rle_compress(packed, 3)
        if len(rle) < len(packed):
            encoded.append([1, 1, rle])
        else:
            encoded.append([0, 1, packed])

    frames_chunk_len = sum(struct.calcsize(_FRAME_FORMAT) + len(e[2])
                           for e in encoded)
    max_encoded_len = max(len(e[2]) for e in encoded)
    sections = [{"name": "default", "start": 0, "end": len(frames) - 1}]
    sections_chunk_len = sum(struct.calcsize(_SECTION_FORMAT)
                             + len(s["name"]) + 1 for s in sections)

    header_len = struct.calcsize(_HEADER_FORMAT)
    display_frame_start = []
    offs = header_len + sections_chunk_len
    for _enc, duration, data in encoded:
        for disp_offset in range(duration, 0, -1):
            display_frame_start.append((offs, disp_offset))
        offs += struct.calcsize(_FRAME_FORMAT) + len(data)

    out = bytearray()
    out += struct.pack(
        _HEADER_FORMAT, b"bicycle0", 0, w, h, 0, fps,
        max_encoded_len, 0, sections_chunk_len, frames_chunk_len,
        len(sections), len(encoded), len(frames))
    for s in sections:
        frame_offs, duration_override = display_frame_start[s["start"]]
        out += struct.pack(_SECTION_FORMAT, s["start"], s["end"],
                           frame_offs, duration_override)
        out += s["name"].encode() + b"\0"
    for enc, duration, data in encoded:
        out += struct.pack(_FRAME_FORMAT, enc, duration, len(data)) + data
    return bytes(out)


# ------------------------------------------------ service-status art

def _plate_ramp(hexc):
    base = _hex_rgb(hexc)
    return {
        "spec": tuple(round(v + (255 - v) * 0.60) for v in base),
        "top": _scale(base, 1.15),
        "bot": _scale(base, 0.47),
        "lift": _scale(base, 0.62),
    }


WORD_INK_TOP = 2    # status word ink rows 2-8; marquee inks 10-13 below it
WORD_X = 19         # left-aligned after the bullet slot, firmware style


def _plate_pixels(hexc, hazard=False):
    """The busy-mode box as an RGBA grid: 1px side inset (the stock plates
    span cols 1-70), 1px specular top, vertical ramp, lifted bottom edge,
    3px corner vignette, radius-5 corners. `hazard` lays dashed stripes on
    the top and bottom rows (keep_out grammar)."""
    pal = _plate_ramp(hexc)
    w, h, r = 70, 16, 5
    dark = (24, 20, 2, 255)
    rows = []
    for y in range(h):
        if y == 0:
            base = pal["spec"]
        elif y == h - 1:
            base = pal["lift"]
        else:
            base = _lerp(pal["top"], pal["bot"], (y - 1) / (h - 2))
        row = [(0, 0, 0, 0)]  # left inset column
        for x in range(w):
            cx, cy = min(x, w - 1 - x), min(y, h - 1 - y)
            if cx < r and cy < r and (r - cx) ** 2 + (r - cy) ** 2 > r * r:
                row.append((0, 0, 0, 0))
                continue
            edge = min(cx, cy)
            scale = (0.25, 0.5, 0.75)[edge] if edge < 3 else 1.0
            if hazard and y in (0, h - 1) and ((x + y) // 4) % 2:
                row.append(dark)
            else:
                row.append(tuple(round(c * scale) for c in base) + (255,))
        row.append((0, 0, 0, 0))  # right inset column
        rows.append(row)
    return rows


def make_status_screen(hexc, word, font="bold", motion="breathe",
                       hazard=False):
    """A status page authored the way the stock busy-mode screens are: the
    WORD baked at ink rows 2-8 (left x=19, firmware shadow) onto a shaded
    plate — compiled as a short LOOPING .anim so each screen keeps exactly
    one living element, like the firmware's own modes:
      breathe — calm brightness sine (dnd grammar; red plates)
      crawl   — hazard dashes marching along rows 0/15 (keep_out grammar)
      sweep   — a soft gradient drifting through the fill (the REROUTED
                look)
    Returns {"anim": bytes, "png": frame-0 PNG} — the PNG feeds previews.
    Baking sidesteps the text elements' 2-row font leading (measured on
    hardware), which once pushed a line clean off the panel."""
    pal = _plate_ramp(hexc)
    table = {"bold": BULLET_GLYPHS, "condensed": CONDENSED_GLYPHS}[font]
    ink = set()
    x = WORD_X
    for ch in word.upper():
        if ch == " ":
            x += 4
            continue
        g = table[ch]
        for gy, grow in enumerate(g):
            for gx, c in enumerate(grow):
                if c == "#":
                    ink.add((x + gx, WORD_INK_TOP + gy))
        x += len(g[0]) + 1
    if x - 1 > 70:
        raise SystemExit(f"status word {word!r} is {x - 1}px — over the "
                         "plate (52px text area)")
    shadow = {(px, py + 1) for px, py in ink
              if (px, py + 1) not in ink and py + 1 < 16}

    n, fps = {"breathe": (32, 16), "crawl": (8, 8),
              "sweep": (24, 15)}[motion]
    dark = (24, 20, 2)
    w, h, r = 70, 16, 5
    frames_rgb = []
    frame0_rgba = None
    for i in range(n):
        t = i / n
        k = 0.93 + 0.07 * math.sin(t * 2 * math.pi) if motion == "breathe" \
            else 1.0
        rgba = []
        for y in range(h):
            if y == 0:
                base = pal["spec"]
            elif y == h - 1:
                base = pal["lift"]
            else:
                base = _lerp(pal["top"], pal["bot"], (y - 1) / (h - 2))
            row = [(0, 0, 0, 0)]
            for x in range(w):
                cx, cy = min(x, w - 1 - x), min(y, h - 1 - y)
                if cx < r and cy < r and \
                        (r - cx) ** 2 + (r - cy) ** 2 > r * r:
                    row.append((0, 0, 0, 0))
                    continue
                edge = min(cx, cy)
                scale = (0.25, 0.5, 0.75)[edge] if edge < 3 else 1.0
                c = base
                if motion == "sweep" and 0 < y < h - 1:
                    v = 0.75 + 0.25 * math.sin(
                        (x + y * 2 - i * 3) * math.pi / 12)
                    c = tuple(min(255, round(cc * v)) for cc in c)
                if hazard and y in (0, h - 1):
                    off = i if motion == "crawl" else 0
                    if ((x + off + y) // 4) % 2:
                        row.append((*dark, 255))
                        continue
                row.append(tuple(min(255, round(cc * scale * k))
                                 for cc in c) + (255,))
            row.append((0, 0, 0, 0))
            rgba.append(row)
        for px, py in shadow:
            if rgba[py][px][3]:
                pp = rgba[py][px]
                rgba[py][px] = tuple(round(v * 0.4) for v in pp[:3]) + (255,)
        for px, py in ink:
            if rgba[py][px][3]:
                rgba[py][px] = (255, 255, 255, 255)
        if frame0_rgba is None:
            frame0_rgba = rgba
        frames_rgb.append(b"".join(
            bytes(v for pp in row for v in pp[:3]) for row in rgba))
    anim = anim_encode(frames_rgb, 72, 16, fps=fps)
    if len(anim) > 135_000:
        raise SystemExit(f"status anim {word!r} is {len(anim)}B — over the "
                         "cloud relay's ~150KiB request cap")
    return {"anim": anim, "png": png_encode(72, 16, frame0_rgba)}


def wash_anim_frames():
    """The page-swap wash, transition_select grammar: a soft amber ring
    blooms from the top edge while a wash rises fast and decays, ending
    black — the swap underneath lands invisibly (departure-flash trick)."""
    amber = (255, 176, 0)
    frames = []
    n = int(WASH_SECS * FPS)
    for i in range(n):
        t = i / FPS
        ring_r = 4 + (t / WASH_SECS) * 82
        ring_gain = max(0.0, 1.0 - t / (WASH_SECS * 0.75))
        wash = 0.85 * min(1.0, t / 0.10) * math.exp(-t / 0.22)
        fade = 1.0 if t < WASH_SECS - 0.15 else \
            max(0.0, (WASH_SECS - t) / 0.15)
        row_out = []
        for y in range(16):
            row = []
            for x in range(72):
                d = math.hypot(x - 36, y * 2.6)
                g = math.exp(-((d - ring_r) ** 2) / 50.0) * ring_gain + wash
                g = min(1.0, g) * fade
                row.append(tuple(min(255, round(c * g)) for c in amber))
            row_out.append(row)
        frames.append(b"".join(bytes(v for px in row for v in px)
                               for row in row_out))
    frames.append(b"\x00" * (72 * 16 * 3))  # end black, hold
    return frames


def text_width(text, font):
    """Pixel width of a status string, from the same glyph tables the
    device fonts were parsed into (status screens are all-caps)."""
    table = {"bold": BULLET_GLYPHS, "tiny": TINY_GLYPHS,
             "condensed": CONDENSED_GLYPHS, "small": SMALL_GLYPHS,
             "extra_large": XL_GLYPHS}[font]
    w = 0
    for ch in text.upper():
        if ch == " ":
            w += 3 if font == "tiny" else 4
        else:
            g = table.get(ch)
            # unknown chars (punctuation) get a safe overestimate so a
            # marquee pass never gets cut short
            w += (len(g[0]) + 1) if g else 4
    return max(w - 1, 1)


def build_status_assets():
    """The six baked status screens (looping anims + frame-0 PNGs) and
    the wash anim, content-hash named like every other asset. `quiet` is
    the nightly service-over plate — same words as a suspension but calm
    slate blue: the last bus leaving on schedule is not an emergency."""
    out = {}
    for key, screen in (
            ("susp", make_status_screen("#7E1416", "NO BUSES",
                                        font="condensed")),
            ("planned", make_status_screen("#FCC30B", "PLANNED",
                                           motion="crawl", hazard=True)),
            ("delayed", make_status_screen("#7E1416", "DELAYED")),
            ("alertpg", make_status_screen("#7E1416", "ALERT")),
            ("quiet", make_status_screen("#23445F", "NO BUSES",
                                         font="condensed")),
            ("detour", make_status_screen("#123A7A", "DETOUR",
                                          motion="sweep"))):
        digest = hashlib.sha256(screen["anim"]).hexdigest()[:8]
        out[key] = {"name": f"st_{key}-{digest}.anim",
                    "bytes": screen["anim"], "png": screen["png"]}
    wash = anim_encode(wash_anim_frames(), 72, 16)
    out["wash"] = {"name": f"wash-{hashlib.sha256(wash).hexdigest()[:8]}"
                           ".anim", "bytes": wash}
    return out


def build_assets(designators):
    """Generate per-route art; names carry a content hash so art-pipeline
    changes never collide with stale files cached on the device."""
    assets = {}
    for d in designators:
        bullet = make_bullet(d)
        anim = anim_encode(flash_anim_frames(d), 72, 16)
        assets[d] = {
            "bullet_name": f"bullet_{d}-{hashlib.sha256(bullet).hexdigest()[:8]}.png",
            "bullet": bullet,
            "flash_name": f"flash_{d}-{hashlib.sha256(anim).hexdigest()[:8]}.anim",
            "flash": anim,
        }
    return assets


# ---------------------------------------------------------------- device I/O

class Target:
    def __init__(self, name, base, api_prefix, headers, ws_uri):
        self.name = name
        self.base = base
        self.api_prefix = api_prefix
        self.headers = headers
        self.ws_uri = ws_uri

    def url(self, path):
        return f"{self.base}{self.api_prefix}{path}"


def make_targets():
    targets = {
        "usb": Target("usb", USB_URL, "/api", {},
                      f"ws://{USB_URL.removeprefix('http://')}/api/status/ws"),
        "wifi": Target("wifi", WIFI_URL, "/api",
                       {"X-API-Token": WIFI_TOKEN}, None),
    }
    if CLOUD_TOKEN:
        # cloud WS handshake rejects device API tokens (needs an account
        # session), so no dial stream on this target
        targets["cloud"] = Target(
            "cloud", CLOUD_URL, "/busybar",
            {"Authorization": f"Bearer {CLOUD_TOKEN}"}, None)
    return targets


class Bar:
    def __init__(self):
        self.t = None
        self.s = requests.Session()

    def connect(self, host_override=None):
        if host_override:
            # busybar-manager mode: plain HTTP to the manager's proxy, which
            # forwards to the bar (and injects the variation's priority)
            t = Target("manager", f"http://{host_override}", "/api", {},
                       WS_OVERRIDE or None)
            r = self.s.get(t.url("/version"), headers=t.headers, timeout=10)
            r.raise_for_status()
            self.t = t
            print(f"connected via manager proxy at {host_override}"
                  f" ({'dial' if t.ws_uri else 'static'} mode)")
            return
        targets = make_targets()
        order = ["usb", "wifi", "cloud"] if TARGET == "auto" else [TARGET]
        errors = []
        for name in order:
            t = targets.get(name)
            if t is None:
                errors.append(f"{name}: not configured "
                              "(cloud needs BUSYBAR_CLOUD_TOKEN)")
                continue
            try:
                r = self.s.get(t.url("/version"), headers=t.headers,
                               timeout=3 if name == "usb" else 10)
                r.raise_for_status()
                if WS_OVERRIDE:
                    t.ws_uri = WS_OVERRIDE
                self.t = t
                print(f"connected to BUSY Bar via {name}"
                      f" ({'dial' if t.ws_uri else 'static'} mode)")
                return
            except requests.RequestException as e:
                errors.append(f"{name}: {e}")
        raise SystemExit("no reachable BUSY Bar target:\n  " +
                         "\n  ".join(errors))

    def upload_assets(self, assets, extra=None):
        files = {}
        for a in assets.values():
            files[a["bullet_name"]] = a["bullet"]
            files[a["flash_name"]] = a["flash"]
        for a in (extra or {}).values():
            files[a["name"]] = a["bytes"]
        # Upload only what's missing at the right size — over the cloud relay
        # re-pushing every ~16KB anim on each start is a visible stall.
        try:
            r = self.s.get(self.t.url("/storage/list"),
                           params={"path": f"/ext/user_assets/{APP_NAME}"},
                           headers=self.t.headers, timeout=10)
            if r.ok:
                present = {e["name"]: e.get("size")
                           for e in r.json().get("list", [])}
                files = {n: b for n, b in files.items()
                         if present.get(n) != len(b)}
        except (requests.RequestException, ValueError):
            pass  # unreadable listing just means we upload everything
        for filename, blob in files.items():
            r = self.s.post(
                self.t.url("/assets/upload"),
                params={"application_name": APP_NAME, "file": filename},
                headers={**self.t.headers,
                         "Content-Type": "application/octet-stream"},
                data=blob, timeout=20)
            r.raise_for_status()
        if files:
            print(f"uploaded {len(files)} asset(s)")

    def draw(self, elements):
        """Push elements. Returns True if drawn, False if the Bar is busy
        with a higher-priority app (409) — i.e. the user is elsewhere."""
        body = {"application_name": APP_NAME, "priority": PRIORITY,
                "elements": elements}
        r = self.s.post(self.t.url("/display/draw"),
                        headers=self.t.headers, json=body, timeout=15)
        if r.status_code == 409:
            return False
        if r.status_code == 400:
            # a stale element with a conflicting type wedges every draw
            # (the firmware 400s type changes on an existing id) — clear
            # our canvas and retry once to self-heal
            self.clear()
            r = self.s.post(self.t.url("/display/draw"),
                            headers=self.t.headers, json=body, timeout=15)
            if r.status_code == 409:
                return False
        r.raise_for_status()
        return True

    def clear(self):
        self.s.delete(
            self.t.url("/display/draw"), headers=self.t.headers,
            params={"application_name": APP_NAME}, timeout=15,
        ).raise_for_status()


# ------------------------------------------------------------- preview bar

class PreviewBar:
    """A Bar stand-in that composites every draw into PNG frames on disk,
    so the whole app — screens, plates, demos — can be eyeballed with no
    hardware. It renders through the same generated assets and glyph
    tables the device gets; font leading and marquee motion are
    approximated (marquees render their head, static)."""

    _FONTS = {"extra_large": XL_GLYPHS, "bold": BULLET_GLYPHS,
              "condensed": CONDENSED_GLYPHS, "small": SMALL_GLYPHS,
              "tiny": TINY_GLYPHS}

    class _T:
        name = "preview"
        ws_uri = None

    scale = 8  # site/capture tooling sets 1 for raw 72x16 frames

    def __init__(self, outdir="preview"):
        self.t = self._T()
        self.outdir = outdir
        self.files = {}     # uploaded asset name -> bytes
        self.canvas = {}    # element id -> element (device merge-by-id)
        self.order = []
        self.n = 0
        os.makedirs(outdir, exist_ok=True)

    def upload_assets(self, assets, extra=None):
        for a in assets.values():
            self.files[a["bullet_name"]] = a["bullet"]
            self.files[a["flash_name"]] = a["flash"]
        for a in (extra or {}).values():
            self.files[a["name"]] = a["bytes"]

    def clear(self):
        self.canvas.clear()
        self.order = []

    def draw(self, elements):
        for el in elements:
            if el["id"] not in self.canvas:
                self.order.append(el["id"])
            self.canvas[el["id"]] = el
        self._save()
        return True

    # -- decoding our own asset formats ------------------------------------

    @staticmethod
    def _decode_png(data):
        """png_encode's output back to RGBA rows (filter 0 only)."""
        i, w, h, idat = 8, 0, 0, b""
        while i < len(data):
            ln = int.from_bytes(data[i:i + 4], "big")
            tag = data[i + 4:i + 8]
            if tag == b"IHDR":
                w = int.from_bytes(data[i + 8:i + 12], "big")
                h = int.from_bytes(data[i + 12:i + 16], "big")
            elif tag == b"IDAT":
                idat += data[i + 8:i + 8 + ln]
            i += 12 + ln
        raw = zlib.decompress(idat)
        stride = w * 4 + 1
        rows = []
        for y in range(h):
            line = raw[y * stride:(y + 1) * stride]
            if line[0] != 0:
                return w, h, None  # not our encoder's output
            rows.append([tuple(line[1 + x * 4:5 + x * 4]) for x in range(w)])
        return w, h, rows

    @staticmethod
    def _anim_frame(blob):
        """One display frame of a bicycle0 .anim — a third of the way in,
        past any sweep — as (w, h, RGB rows)."""
        hdr = struct.unpack_from(_HEADER_FORMAT, blob, 0)
        w, h, sections_len, n_encoded, n_frames = (
            hdr[2], hdr[3], hdr[8], hdr[11], hdr[12])
        target = max(0, n_frames // 3)
        i = struct.calcsize(_HEADER_FORMAT) + sections_len
        fmt = struct.calcsize(_FRAME_FORMAT)
        shown = -1
        for _ in range(n_encoded):
            enc, duration, ln = struct.unpack_from(_FRAME_FORMAT, blob, i)
            data = blob[i + fmt:i + fmt + ln]
            i += fmt + ln
            shown += duration
            if shown < target and _ < n_encoded - 1:
                continue
            if enc:  # RLE, block size 3
                out = bytearray()
                j = 0
                while j < len(data):
                    c = data[j]; j += 1
                    if c & 0x80:
                        cnt = (c & 0x7F) * 3
                        out += data[j:j + cnt]
                        j += cnt
                    else:
                        out += data[j:j + 3] * c
                        j += 3
                data = bytes(out)
            rows = []
            for y in range(h):
                row = []
                for x in range(w):
                    o = (y * w + x) * 3
                    b_, g_, r_ = data[o], data[o + 1], data[o + 2]
                    row.append((r_, g_, b_))
                rows.append(row)
            return w, h, rows
        return w, h, [[(0, 0, 0)] * w for _ in range(h)]

    # -- compositing -------------------------------------------------------

    @staticmethod
    def _color(spec, fallback=(255, 255, 255)):
        try:
            return _hex_rgb(spec[:7])
        except Exception:
            return fallback

    def _blit_text(self, grid, el):
        font = el.get("font", "bold")
        table = self._FONTS.get(font, BULLET_GLYPHS)
        text = str(el.get("text", "")).upper()
        color = self._color(el.get("color", "#FFFFFFFF"))
        gh = max((len(g) for g in table.values()), default=7)
        tw = text_width(text, font if font in (
            "bold", "tiny", "condensed", "small", "extra_large") else "bold")
        x, y = el.get("x", 0), el.get("y", 0)
        align = el.get("align", "top_left")
        if align == "center":
            x0, y0 = x - tw // 2, y - gh // 2
        elif align == "mid_right":
            x0, y0 = x - tw, y - gh // 2
        elif align == "bottom_left":
            x0, y0 = x, y - gh + 1
        else:  # top_left — the firmware's ~2-row label leading, measured
            x0, y0 = x, y + 2
        clip_lo = x if "width" in el else None
        clip_hi = x + el["width"] if "width" in el else None
        cx = x0
        for ch in text:
            if ch == " ":
                cx += 3 if font == "tiny" else 4
                continue
            g = table.get(ch)
            if not g:
                cx += 4
                continue
            for gy, grow in enumerate(g):
                for gx, c in enumerate(grow):
                    px, py = cx + gx, y0 + gy
                    if c != "#" or not (0 <= px < 72 and 0 <= py < 16):
                        continue
                    if clip_lo is not None and not clip_lo <= px < clip_hi:
                        continue
                    grid[py][px] = color
            cx += len(g[0]) + 1

    def _blit_image(self, grid, el, rows):
        x0, y0 = el.get("x", 0), el.get("y", 0)
        for gy, row in enumerate(rows):
            for gx, px in enumerate(row):
                tx, ty = x0 + gx, y0 + gy
                if not (0 <= tx < 72 and 0 <= ty < 16):
                    continue
                if len(px) == 4:
                    a = px[3] / 255.0
                    if a == 0:
                        continue
                    base = grid[ty][tx]
                    grid[ty][tx] = tuple(
                        round(px[i] * a + base[i] * (1 - a))
                        for i in range(3))
                else:
                    grid[ty][tx] = px[:3]

    def _save(self):
        grid = [[(0, 0, 0)] * 72 for _ in range(16)]
        grid = [list(r) for r in grid]
        for eid in self.order:
            el = self.canvas[eid]
            kind = el.get("type")
            if kind == "text":
                self._blit_text(grid, el)
            elif kind in ("image", "animation"):
                blob = self.files.get(el.get("path", ""))
                if not blob:
                    continue
                if kind == "image":
                    _w, _h, rows = self._decode_png(blob)
                else:
                    _w, _h, rows = self._anim_frame(blob)
                if rows:
                    self._blit_image(grid, el, rows)
            elif kind == "rectangle":
                color = self._color((el.get("fill_colors") or ["#FFFFFF"])[0])
                for gy in range(el.get("height", 1)):
                    for gx in range(el.get("width", 1)):
                        tx, ty = el.get("x", 0) + gx, el.get("y", 0) + gy
                        if 0 <= tx < 72 and 0 <= ty < 16:
                            grid[ty][tx] = color
        scale = self.scale
        big = [[(*grid[y // scale][x // scale], 255)
                for x in range(72 * scale)] for y in range(16 * scale)]
        blob = png_encode(72 * scale, 16 * scale, big)
        self.n += 1
        path = os.path.join(self.outdir, f"{self.n:03d}.png")
        with open(path, "wb") as f:
            f.write(blob)
        with open(os.path.join(self.outdir, "latest.png"), "wb") as f:
            f.write(blob)
        print(f"preview -> {path}")


# -------------------------------------------------------------- CATA realtime

def _walk_fields(buf):
    """Yield (field_number, wire_type, value) for one protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        tag = 0
        shift = 0
        while True:
            b = buf[i]; i += 1
            tag |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        field, wire = tag >> 3, tag & 7
        if wire == 0:  # varint
            v = 0; shift = 0
            while True:
                b = buf[i]; i += 1
                v |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            yield field, wire, v
        elif wire == 1:  # fixed64
            yield field, wire, buf[i:i + 8]; i += 8
        elif wire == 2:  # length-delimited
            ln = 0; shift = 0
            while True:
                b = buf[i]; i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            yield field, wire, buf[i:i + ln]; i += ln
        elif wire == 5:  # fixed32
            yield field, wire, buf[i:i + 4]; i += 4
        else:
            return


def decode_trip_updates(buf, stops, want_desigs):
    """Hand-rolled GTFS-realtime decode (same trick as the dial stream —
    no protobuf dependency). Yields (epoch, route_id, trip_id, delay_secs)
    for watched stops. Field numbers per gtfs-realtime.proto:
      FeedMessage.entity=2 -> FeedEntity.trip_update=3 ->
        TripUpdate.trip=1 {trip_id=1, route_id=5}
        TripUpdate.stop_time_update=2 {arrival=2{delay=1,time=2},
                                       departure=3{delay=1,time=2},
                                       stop_id=4}
    """
    for f, w, entity in _walk_fields(buf):
        if f != 2 or w != 2:
            continue
        for f2, w2, tu in _walk_fields(entity):
            if f2 != 3 or w2 != 2:
                continue
            trip_id, route_id, hits = "", "", []
            for f3, w3, v3 in _walk_fields(tu):
                if f3 == 1 and w3 == 2:  # TripDescriptor
                    for f4, w4, v4 in _walk_fields(v3):
                        if f4 == 1 and w4 == 2:
                            trip_id = v4.decode("utf-8", "replace")
                        elif f4 == 5 and w4 == 2:
                            route_id = v4.decode("utf-8", "replace")
                elif f3 == 2 and w3 == 2:  # StopTimeUpdate
                    stop, dep, arr, delay = "", 0, 0, 0
                    for f4, w4, v4 in _walk_fields(v3):
                        if f4 == 4 and w4 == 2:
                            stop = v4.decode("utf-8", "replace")
                        elif f4 in (2, 3) and w4 == 2:  # arrival / departure
                            for f5, w5, v5 in _walk_fields(v4):
                                if f5 == 2 and w5 == 0:
                                    if f4 == 3:
                                        dep = v5
                                    else:
                                        arr = v5
                                elif f5 == 1 and w5 == 0:
                                    # delay is a plain int32: negative
                                    # values arrive as 64-bit two's
                                    # complement varints
                                    if v5 >= 1 << 63:
                                        v5 -= 1 << 64
                                    delay = v5
                    if stop in stops:
                        hits.append((dep or arr, delay))
            if route_id and designator(route_id) in want_desigs:
                for t, delay in hits:
                    if t:
                        yield t, route_id, trip_id, delay


def decode_status(buf, stops):
    """Status pass over the VehiclePositions feed: held buses and (bonus)
    occupancy. Held = STOPPED_AT whose position timestamp lags the FEED's
    own header timestamp — comparing two feed clocks (not wall clock)
    means a stale snapshot can't mark the whole fleet as held; if the
    feed itself is stale we skip held detection entirely. Returns
    (held {trip_id: (secs, stop_id)}, occupancy {trip_id: enum})."""
    now = time.time()
    feed_ts = 0
    held, occupancy = {}, {}
    for f, w, ent in _walk_fields(buf):
        if f == 1 and w == 2:  # FeedHeader{gtfs_version=1, ..., timestamp=3}
            for f2, w2, v2 in _walk_fields(ent):
                if f2 == 3 and w2 == 0:
                    feed_ts = v2
            continue
        if f != 2 or w != 2:
            continue
        for f2, w2, v2 in _walk_fields(ent):
            if f2 != 4 or w2 != 2:  # VehiclePosition
                continue
            trip_id, stop = "", ""
            status = ts = occ = None
            for f3, w3, v3 in _walk_fields(v2):
                if f3 == 1 and w3 == 2:
                    for f4, w4, v4 in _walk_fields(v3):
                        if f4 == 1 and w4 == 2:
                            trip_id = v4.decode("utf-8", "replace")
                elif f3 == 4 and w3 == 0:
                    status = v3
                elif f3 == 5 and w3 == 0:
                    ts = v3
                elif f3 == 7 and w3 == 2:
                    stop = v3.decode("utf-8", "replace")
                elif f3 == 9 and w3 == 0:
                    occ = v3
            if not trip_id:
                continue
            if occ is not None:
                occupancy[trip_id] = occ
            fresh = feed_ts and now - feed_ts < 120
            if fresh and status == 1 and ts \
                    and feed_ts - ts > HELD_AFTER_SECS:
                held[trip_id] = (feed_ts - ts, stop)
    return held, occupancy


def fetch_arrivals(group, schedule):
    """Live + scheduled departures for one group, merged by trip identity:
    ([(epoch, route_id, trip_id, delay, live, is_last)] sorted,
    {"held": ..., "occupancy": ...}).

    Realtime covers roughly the next 90 minutes of dispatched trips; the
    static schedule pads the list beyond that so a 32-minute-headway route
    still fills the position dots. A near-term scheduled trip the realtime
    feed should already know (inside SCHED_MERGE_MIN) is left out — if
    Avail isn't predicting it, it is more likely cancelled than merely
    undispatched — unless realtime came back empty for the group.
    """
    now = time.time()
    stops = set(group["stops"])
    want = set(group["designators"])
    per_trip = {}
    held, occupancy = {}, {}
    try:
        r = requests.get(rt_url("TripUpdates"), timeout=15)
        r.raise_for_status()
        for t, route_id, trip_id, delay in decode_trip_updates(
                r.content, stops, want):
            if t <= now - 15:
                continue
            key = trip_id or f"{route_id}@{t}"
            # a trip that touches two watched stops counts once, at its
            # earliest watched departure
            if key not in per_trip or t < per_trip[key][0]:
                per_trip[key] = (t, route_id, trip_id, delay, True)
    except requests.RequestException as e:
        print(f"[{time.strftime('%H:%M:%S')}] trip feed trouble: {e}",
              file=sys.stderr)
    if ALERTS_ON:
        try:
            rv = requests.get(rt_url("VehiclePositions"), timeout=15)
            rv.raise_for_status()
            held, occupancy = decode_status(rv.content, stops)
        except requests.RequestException as e:
            print(f"[{time.strftime('%H:%M:%S')}] vehicle feed trouble: "
                  f"{e}", file=sys.stderr)

    sched_floor = now + (SCHED_MERGE_MIN * 60 if per_trip else 0)
    last_marks = set()
    for t, route_id, trip_id, is_last in schedule.departures(
            group, now, horizon_secs=6 * 3600):
        if is_last:
            last_marks.add(trip_id)
        if trip_id in per_trip or t < sched_floor:
            continue
        per_trip[trip_id] = (t, route_id, trip_id, 0, False)

    out = [(t, rid, trip, delay, live, trip in last_marks)
           for (t, rid, trip, delay, live) in sorted(per_trip.values())]
    return out[:MAX_ARRIVALS], {"held": held, "occupancy": occupancy}


def plain_text(text):
    """Alert copy -> device-safe ASCII: strip the [G]-style bullet tokens
    and icon markers, fold typographic punctuation."""
    text = re.sub(r"\[([0-9A-Z]+)\]", r"\1", text)
    text = re.sub(r"\[[^\]]+ icon\]\s*", "", text)
    for a, b in (("—", "-"), ("–", "-"), ("•", "-"),
                 ("’", "'"), ("‘", "'"), ("“", '"'),
                 ("”", '"'), (" ", " ")):
        text = text.replace(a, b)
    return text.encode("ascii", "ignore").decode()


INFOPOINT_ROUTES_URL = ("https://realtime.catabus.com/InfoPoint/rest/"
                        "Routes/GetVisibleRoutes")


def _translated(buf):
    """gtfs-realtime TranslatedString -> best text (prefer English)."""
    best = ""
    for f, w, tr in _walk_fields(buf):
        if f != 1 or w != 2:
            continue
        text, lang = "", ""
        for f2, w2, v2 in _walk_fields(tr):
            if f2 == 1 and w2 == 2:
                text = v2.decode("utf-8", "replace")
            elif f2 == 2 and w2 == 2:
                lang = v2.decode("utf-8", "replace")
        if text and (not best or lang.lower().startswith("en")):
            best = text
    return best


def classify_alert(text):
    """CATA's alerts are prose with cause/effect left UNKNOWN, so the
    plate choice keys off wording. Unrecognized phrasing lands on the
    generic ALERT page rather than guessing anything scarier."""
    t = text.lower()
    if any(k in t for k in ("no service", "not operate", "not run",
                            "suspended", "will not service",
                            "not be servic")):
        return "suspension"
    if any(k in t for k in ("detour", "reroute", "closed", "closure",
                            "relocat", "use stop")):
        return "detour"
    if any(k in t for k in ("delay", "running late", "behind schedule")):
        return "delays"
    if any(k in t for k in ("construction", "planned", "will begin",
                            "beginning", "starting")):
        return "planned"
    return "other"


def fetch_alerts(cfg):
    """Currently-active alerts scoped to the configured routes/stops (or
    agency-wide), from the GTFS-realtime Alerts feed plus InfoPoint's
    per-route messages — the latter carry an explicit Detour_Id flag the
    protobuf feed lacks. Returns [{kind, type, head, period, routes}]
    most severe first."""
    now = time.time()
    want_desigs = {d for g in cfg["groups"] for d in g["designators"]}
    want_routes = {r for g in cfg["groups"] for r in g["route_ids"]}
    want_stops = {s for g in cfg["groups"] for s in g["stops"]}
    out, seen = [], set()

    r = requests.get(rt_url("Alerts"), timeout=15)
    r.raise_for_status()
    for f, w, ent in _walk_fields(r.content):
        if f != 2 or w != 2:
            continue
        for f2, w2, al in _walk_fields(ent):
            if f2 != 5 or w2 != 2:  # FeedEntity.alert
                continue
            head = ""
            periods = []
            routes, stops_hit = set(), set()
            agency_wide = False
            for f3, w3, v3 in _walk_fields(al):
                if f3 == 1 and w3 == 2:  # TimeRange{start=1, end=2}
                    start = end = 0
                    for f4, w4, v4 in _walk_fields(v3):
                        if f4 == 1 and w4 == 0:
                            start = v4
                        elif f4 == 2 and w4 == 0:
                            end = v4
                    periods.append((start, end or 2 ** 62))
                elif f3 == 5 and w3 == 2:  # EntitySelector
                    agency = sel_route = sel_stop = ""
                    for f4, w4, v4 in _walk_fields(v3):
                        if f4 == 1 and w4 == 2:
                            agency = v4.decode("utf-8", "replace")
                        elif f4 == 2 and w4 == 2:
                            sel_route = v4.decode("utf-8", "replace")
                        elif f4 == 5 and w4 == 2:
                            sel_stop = v4.decode("utf-8", "replace")
                    if sel_route:
                        routes.add(sel_route)
                    elif sel_stop:
                        stops_hit.add(sel_stop)
                    elif agency:
                        agency_wide = True
                elif f3 == 10 and w3 == 2:  # header_text
                    head = _translated(v3)
            if not head:
                continue
            if periods and not any(s <= now <= e for s, e in periods):
                continue
            if routes or stops_hit:
                if not ((routes & want_routes)
                        or (stops_hit & want_stops)):
                    continue
                hit = sorted({designator(x) for x in routes & want_routes}
                             ) or sorted(want_desigs)
            elif agency_wide:
                hit = sorted(want_desigs)
            else:
                continue
            kind = classify_alert(head)
            key = plain_text(head)[:60]
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": kind, "type": kind,
                        "head": plain_text(head), "period": "",
                        "routes": hit})

    # InfoPoint route messages: usually the same prose, but with an
    # explicit detour flag — and ModeReportLabel exposes states the
    # protobuf feed folds into UNKNOWN. Best-effort only.
    try:
        r = requests.get(INFOPOINT_ROUTES_URL, timeout=15)
        r.raise_for_status()
        for route in r.json():
            ab = (route.get("RouteAbbreviation") or "").upper()
            if ab not in want_desigs:
                continue
            for m in route.get("Messages") or []:
                text = m.get("Message") or m.get("Header") or ""
                key = plain_text(text)[:60]
                if not text or key in seen:
                    continue
                seen.add(key)
                kind = ("detour" if m.get("Detour_Id")
                        else classify_alert(text))
                out.append({"kind": kind, "type": kind,
                            "head": plain_text(text), "period": "",
                            "routes": [ab]})
    except (requests.RequestException, ValueError) as e:
        print(f"[{time.strftime('%H:%M:%S')}] infopoint messages: {e}",
              file=sys.stderr)

    rank = {"suspension": 0, "delays": 1, "detour": 2, "planned": 3,
            "other": 4}
    out.sort(key=lambda a: rank[a["kind"]])
    return out


# ----------------------------------------------------------------- rendering

def asset_desig(assets, route_id):
    """Art key for an arrival: its own designator when we built art for it,
    else its local's (a feed can surface an express we didn't pre-build)."""
    d = designator(route_id)
    return d if d in assets else base_desig(d)


def build_plate_screen(status_assets, screen_key, bullet_name,
                       marquee=None, marquee_color=WHITE):
    """A status page: the baked looping screen (plate + word + its one
    living element), the route bullet in the icon slot, and an optional
    marquee in the small face — top_left y=8 inks rows 10-14 with the
    measured 2-row leading. Element ids stay type-stable."""
    els = [{"id": "plate", "type": "animation",
            "path": status_assets[screen_key]["name"],
            "x": 0, "y": 0, "loop": True, "timeout": ELEMENT_TIMEOUT}]
    if bullet_name:
        els.append({"id": "bullet", "type": "image", "path": bullet_name,
                    "x": 1, "y": 0, "timeout": ELEMENT_TIMEOUT})
    if marquee:
        el = {"id": "mq", "type": "text", "text": marquee,
              "font": "small", "color": marquee_color,
              "x": WORD_X, "y": 8, "align": "top_left",
              "timeout": ELEMENT_TIMEOUT}
        win = 69 - WORD_X + 1
        if text_width(marquee, "small") > win:
            # scroll props only when the line actually overflows — a label
            # that fits must not carry them (firmware scrolls it anyway)
            el.update({"width": win, "scroll_rate": MARQUEE_RATE})
        els.append(el)
    return els


def marquee_pass_secs(text):
    """One full circular-scroll cycle for the small in-plate marquee — the
    LVGL formula: (text_px + 15px wait gap) * 60000 / rate."""
    return (text_width(text, "small") + 15) * 60.0 / MARQUEE_RATE


def build_screen(cfg, assets, arrivals, index, offset=0, alert_dot=False):
    """One arrival card, optionally shifted vertically by `offset` px."""
    els = []
    if not arrivals:
        routes_label = "/".join(
            d for d in cfg["designators"] if not is_express(d)) \
            or "/".join(cfg["designators"])
        els.append({
            "id": "msg", "type": "text",
            "text": f"No {cfg['dir_word']} {routes_label} buses",
            "font": "small", "color": WHITE, "align": "center",
            "x": 36, "y": 8, "width": 72, "scroll_rate": 1500,
            "timeout": ELEMENT_TIMEOUT,
        })
        return els

    index = max(0, min(index, len(arrivals) - 1))
    dep_time, route = arrivals[index][0], arrivals[index][1]
    is_last = arrivals[index][5]
    mins = int(max(0, dep_time - time.time()) // 60)

    els.append({
        "id": "bullet", "type": "image",
        "path": assets[asset_desig(assets, route)]["bullet_name"],
        "x": 1, "y": 0 + offset, "timeout": ELEMENT_TIMEOUT,
    })
    if alert_dot:
        # a live service alert exists: quiet amber corner dot on the
        # bullet; the full story plays as the periodic alert page
        els.append({
            "id": "adot", "type": "rectangle", "x": 12, "y": 0 + offset,
            "width": 3, "height": 3, "fill": "solid",
            "fill_colors": [AMBER], "border_width": 0,
            "timeout": ELEMENT_TIMEOUT,
        })
    if mins == 0:
        els.append({
            "id": "num", "type": "text", "text": "NOW",
            "font": "extra_large", "color": WHITE, "align": "center",
            "x": 42, "y": 8 + offset, "timeout": ELEMENT_TIMEOUT,
        })
        # park off-screen, same type: the firmware 400s a type change on an
        # existing id, so "unit" must always stay a text element
        els.append({
            "id": "unit", "type": "text", "text": "min",
            "font": "bold", "color": WHITE, "align": "bottom_left",
            "x": 37, "y": -30, "timeout": ELEMENT_TIMEOUT,
        })
    else:
        els.append({
            "id": "num", "type": "text", "text": str(mins),
            "font": "extra_large", "color": WHITE, "align": "mid_right",
            "x": 34, "y": 8 + offset, "timeout": ELEMENT_TIMEOUT,
        })
        els.append({
            "id": "unit", "type": "text", "text": "min",
            "font": "bold", "color": WHITE, "align": "bottom_left",
            "x": 37, "y": 15 + offset, "timeout": ELEMENT_TIMEOUT,
        })
    # the day's final scheduled departure carries a LAST tag over the
    # minutes; parked (same type) otherwise — the firmware 400s a type
    # change on an existing id. Skipped in the NOW case, where the big
    # centered NOW owns that region (the departure flash follows anyway).
    els.append({
        "id": "last", "type": "text", "text": "LAST",
        "font": "tiny", "color": AMBER, "align": "top_left",
        "x": 37, "y": (0 + offset) if (is_last and mins > 0) else -30,
        "timeout": ELEMENT_TIMEOUT,
    })
    # position dots, two 1px columns down the right edge: the right column
    # always shows each upcoming bus's route color; the single white pixel
    # in the left column marks the departure currently on screen (one
    # element that moves — merge-by-id keeps it from leaving trails)
    for i, (_t, r, *_rest) in enumerate(arrivals):
        els.append({
            "id": f"dot{i}", "type": "rectangle",
            "x": 71, "y": i * 2, "width": 1, "height": 1,
            "fill": "solid",
            "fill_colors": [line_color(designator(r)) + "FF"],
            "border_width": 0, "timeout": ELEMENT_TIMEOUT,
        })
    els.append({
        "id": "mark", "type": "rectangle",
        "x": 70, "y": index * 2, "width": 1, "height": 1,
        "fill": "solid", "fill_colors": [WHITE],
        "border_width": 0, "timeout": ELEMENT_TIMEOUT,
    })
    return els


def build_flash_anim(assets, desig):
    """Departure flash element: the compiled per-route .anim. One push; the
    device runs the sweep/hold/fade at 60fps and holds the final black frame,
    so the clear that follows is invisible. Never re-push this id while it
    plays — a re-push restarts the animation."""
    return [{
        "id": "flash_anim", "type": "animation",
        "path": assets[desig]["flash_name"],
        "x": 0, "y": 0, "loop": False, "timeout": ELEMENT_TIMEOUT,
    }]


# ---------------------------------------------------------------- main loops

class App:
    def __init__(self, bar, cfg, assets, status_assets=None,
                 schedule=None):
        self.bar = bar
        self.full_cfg = cfg
        self.groups = cfg["groups"]
        self.cfg = self.groups[0]   # active group; fetcher may swap to the
        #                             fallback on no-service days
        self.schedule = schedule
        self.assets = assets
        self.status_assets = status_assets or {}
        self.arrivals = []
        self.alerts = []            # active alerts for the watched routes
        self.held = {}              # trip_id -> (secs stuck, stop_id)
        self.occupancy = {}         # trip_id -> occupancy enum (logged)
        self.last_alert_page = time.time()  # settle before first interrupt
        self.page_hold_until = 0.0  # alert page on screen until then
        self.stop_names = schedule.stops if schedule else {}
        self.index = 0
        self.blocked = False        # a higher-priority app owns the screen
        self.last_dial = 0.0
        self.dot_count = 0
        self.canvas_mode = None     # "card" | "msg" | "plate_*" | None
        self.shown_key = None       # (dep_time, route, mins) last rendered
        self.lock = asyncio.Lock()  # serializes renders/animations

    # -- rendering ---------------------------------------------------------

    def displayed(self):
        if not self.arrivals:
            return None
        self.index = max(0, min(self.index, len(self.arrivals) - 1))
        return self.arrivals[self.index]

    def _alert(self, *kinds):
        for a in self.alerts:
            if a["kind"] in kinds:
                return a
        return None

    def _status_bullet(self, alert=None):
        """The bullet for a status plate: the alert's own line when we have
        art for it, else the station's first — a C-only suspension at an
        A/C/E station must not fly an A bullet."""
        for r in (alert or {}).get("routes", []):
            if r in self.assets:
                return self.assets[r]["bullet_name"]
        return self.assets[self.cfg["designators"][0]]["bullet_name"]

    def _next_service(self):
        """(designator, marquee) for the quiet plate: the first scheduled
        departure across the groups, primary first on ties."""
        if not self.schedule:
            return None
        now = time.time()
        best = None
        for g in self.groups:
            nxt = self.schedule.next_departure(g, now)
            if nxt and (best is None or nxt[0] < best[0]):
                best = nxt
        if not best:
            return None
        t, rid = best
        desig = designator(rid)
        lt = self.schedule._local(t)
        today = self.schedule._local(now).date()
        if lt.date() == today:
            day = "TODAY"
        elif (lt.date() - today).days == 1:
            day = "TOMORROW"
        else:
            day = lt.strftime("%A").upper()
        clock = (lt.strftime("%I:%M").lstrip("0")
                 + ("A" if lt.hour < 12 else "P"))
        return desig, f"NEXT BUS {desig} {day} {clock}"

    def _screen_elements(self, offset=0):
        """What belongs on screen right now: a status takeover plate, or
        the ordinary card (with the amber dot during live alerts)."""
        if self.status_assets:
            if not self.arrivals:
                a = self._alert("suspension")
                if a:
                    mq = a["head"] + ("   " + a["period"]
                                      if a["period"] else "")
                    return build_plate_screen(
                        self.status_assets, "susp",
                        self._status_bullet(a),
                        marquee=mq.upper()), "plate_susp"
                a = self._alert("planned")
                if a:
                    mq = a["head"] + ("   " + a["period"]
                                      if a["period"] else "")
                    return build_plate_screen(
                        self.status_assets, "planned",
                        self._status_bullet(a),
                        marquee=mq.upper(),
                        marquee_color="#201A02FF"), "plate_plan"
                nxt = self._next_service()
                if nxt:
                    desig, mq = nxt
                    bullet = (self.assets[desig]["bullet_name"]
                              if desig in self.assets else None)
                    return build_plate_screen(
                        self.status_assets, "quiet", bullet,
                        marquee=mq), "plate_quiet"
            shown = self.displayed()
            if shown and shown[2] in self.held:
                secs, stop = self.held[shown[2]]
                name = self.stop_names.get(stop, stop)
                bullet = self.assets[
                    asset_desig(self.assets, shown[1])]["bullet_name"]
                return build_plate_screen(
                    self.status_assets, "delayed", bullet,
                    marquee=f"HELD {int(secs // 60)} MIN AT {name}".upper()
                ), "plate_delay"
            if shown and shown[4] and shown[3] >= DELAY_PLATE_SECS:
                bullet = self.assets[
                    asset_desig(self.assets, shown[1])]["bullet_name"]
                return build_plate_screen(
                    self.status_assets, "delayed", bullet,
                    marquee=f"RUNNING {int(shown[3] // 60)} MIN LATE"
                ), "plate_delay"
        dot = bool(self.status_assets
                   and self._alert("delays", "suspension", "planned"))
        els = build_screen(self.cfg, self.assets, self.arrivals, self.index,
                           offset, alert_dot=dot)
        return els, ("card" if self.arrivals else "msg") + \
            ("+a" if dot else "")

    def _push(self, offset=0):
        """One draw attempt; tracks blocked state. Returns True if drawn."""
        els, mode = self._screen_elements(offset)
        # elements merge by id on the device, so start clean whenever the
        # shape of what we draw changes (fewer dots, card <-> plate <-> msg)
        if (len(self.arrivals) < self.dot_count
                or mode != self.canvas_mode) and self.canvas_mode:
            try:
                self.bar.clear()
            except requests.RequestException:
                pass
        drawn = self.bar.draw(els)
        was_blocked, self.blocked = self.blocked, not drawn
        if drawn:
            self.dot_count = len(self.arrivals)
            self.canvas_mode = mode
            d = self.displayed()
            self.shown_key = d and (d[0], d[1],
                                    int(max(0, d[0] - time.time()) // 60))
            if was_blocked:
                print(f"[{time.strftime('%H:%M:%S')}] screen reclaimed")
        elif not was_blocked:
            print(f"[{time.strftime('%H:%M:%S')}] screen busy — "
                  "waiting politely")
        return drawn

    def _paced(self, fn, *args):
        """Run a draw call and absorb leftover time so animation frames land
        every ~FRAME_SECS regardless of transport latency."""
        t0 = time.time()
        result = fn(*args)
        rest = FRAME_SECS - (time.time() - t0)
        if rest > 0:
            time.sleep(rest)
        return result

    async def render(self):
        async with self.lock:
            try:
                await asyncio.to_thread(self._push)
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] draw error: {e}",
                      file=sys.stderr)

    async def slide_to(self, new_index, direction=1):
        """Eased slide to another arrival (up = next, down = previous)."""
        self.page_hold_until = 0.0  # the dial always wins over alert pages
        async with self.lock:
            if self.blocked or not self.arrivals:
                self.index = new_index
                try:
                    await asyncio.to_thread(self._push)
                except requests.RequestException:
                    pass
                return
            if self.bar.t.name != "usb":
                # dial events can arrive over a forwarded socket (BUSYBAR_WS)
                # while draws still go through the manager/cloud relay, where
                # per-frame eased pushes stretch into seconds — jump-cut
                self.index = new_index
                try:
                    await asyncio.to_thread(self._push)
                except requests.RequestException as e:
                    print(f"[{time.strftime('%H:%M:%S')}] draw error: {e}",
                          file=sys.stderr)
                return
            try:
                sign = 1 if direction >= 0 else -1
                for off in SLIDE_OUT:
                    if not await asyncio.to_thread(
                            self._paced, self._push, off * sign):
                        self.index = new_index
                        return
                self.index = new_index
                for off in SLIDE_IN:
                    if not await asyncio.to_thread(
                            self._paced, self._push, off * sign):
                        return
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] draw error: {e}",
                      file=sys.stderr)

    async def departure_flash(self, departed_route):
        """Full-screen wipe in the departed line's color, then the next
        train slides in."""
        async with self.lock:
            self.index = 0
            if self.blocked:
                try:
                    await asyncio.to_thread(self._push)
                except requests.RequestException:
                    pass
                return
            try:
                if not await asyncio.to_thread(
                        self.bar.draw,
                        build_flash_anim(self.assets,
                                         asset_desig(self.assets,
                                                     departed_route))):
                    self.blocked = True
                    return
                await asyncio.sleep(FLASH_ANIM_SECS)  # device-side 60fps
                await asyncio.to_thread(self.bar.clear)
                self.canvas_mode = None
                self.dot_count = 0
                if self.bar.t.name == "usb":
                    for off in SLIDE_IN:
                        if not await asyncio.to_thread(
                                self._paced, self._push, off):
                            return
                else:
                    # per-frame pushes over the relay stretch the ease into
                    # mush; the screen is already black, so just appear
                    await asyncio.to_thread(self._push)
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] draw error: {e}",
                      file=sys.stderr)

    # -- tasks -------------------------------------------------------------

    @staticmethod
    def _same_train(shown, arrivals):
        """Match the shown train across polls by trip id (stable), falling
        back to route + closest time within 90s for feeds that omit it."""
        if shown[2]:
            for i, a in enumerate(arrivals):
                if a[2] == shown[2]:
                    return i
        best = None
        for i, (t, route, *_rest) in enumerate(arrivals):
            if route == shown[1] and abs(t - shown[0]) < 90:
                if best is None or abs(t - shown[0]) < abs(
                        arrivals[best][0] - shown[0]):
                    best = i
        return best

    def _pick_page(self):
        """The alert page worth interrupting the card for, most urgent
        first: live delays, a track change on the shown train, planned
        work coming up."""
        a = self._alert("delays")
        if a:
            return ("alertpg", a["head"].upper(), WHITE)
        a = self._alert("suspension")
        if a:  # partial suspension while trains still run here
            mq = a["head"] + ("   " + a["period"] if a["period"] else "")
            return ("susp", mq.upper(), "#FFD2CCFF")
        a = self._alert("detour")
        if a:
            mq = a["head"] + ("   " + a["period"] if a["period"] else "")
            return ("detour", mq.upper(), WHITE)
        a = self._alert("planned")
        if a:
            mq = a["head"] + ("   " + a["period"] if a["period"] else "")
            return ("planned", mq.upper(), "#201A02FF")
        return None

    async def _page_recover(self):
        """Land back on the card with a clean canvas, whatever happened.
        Caller holds self.lock."""
        try:
            await asyncio.to_thread(self.bar.clear)
        except requests.RequestException:
            pass
        self.canvas_mode = None
        self.dot_count = 0
        try:
            await asyncio.to_thread(self._push)
        except requests.RequestException:
            pass

    async def alert_page(self):
        """The card ⇄ alert page cycle: an amber wash covers the swap to a
        full-screen plate, the headline makes one marquee pass, the wash
        brings the card back — transition_select grammar throughout. The
        lock is held only for the swap legs; during the hold the page is
        protected by page_hold_until, which user input (the dial) may
        override."""
        self.last_alert_page = time.time()
        page = self._pick_page()
        if not page or not self.status_assets:
            return
        screen_key, marquee, mcolor = page
        shown = self.displayed()
        bullet = (self.assets[asset_desig(self.assets, shown[1])]
                  ["bullet_name"] if shown else self._status_bullet())
        els = build_plate_screen(self.status_assets, screen_key, bullet,
                                 marquee=marquee,
                                 marquee_color=mcolor or "#FFD2CCFF")
        hold = (min(12.0, max(4.0, marquee_pass_secs(marquee) + 0.5))
                if marquee else 5.0)
        wash = [{"id": "wash", "type": "animation",
                 "path": self.status_assets["wash"]["name"],
                 "x": 0, "y": 0, "loop": False,
                 "timeout": ELEMENT_TIMEOUT}]
        async with self.lock:
            if self.blocked:
                return
            try:
                if not await asyncio.to_thread(self.bar.draw, wash):
                    self.blocked = True
                    return
                await asyncio.sleep(WASH_SECS)  # ends black and holds
                await asyncio.to_thread(self.bar.clear)
                self.canvas_mode = None
                self.dot_count = 0
                if not await asyncio.to_thread(self.bar.draw, els):
                    self.blocked = True
                    return
                self.canvas_mode = "alert_page"
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] alert page error: "
                      f"{e}", file=sys.stderr)
                await self._page_recover()
                return
        self.page_hold_until = time.time() + hold
        await asyncio.sleep(hold)
        self.page_hold_until = 0.0
        async with self.lock:
            if self.canvas_mode != "alert_page":
                return  # the dial already took the screen back
            try:
                await asyncio.to_thread(self.bar.draw, wash)
                await asyncio.sleep(WASH_SECS)
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] alert page error: "
                      f"{e}", file=sys.stderr)
            await self._page_recover()
        self.last_alert_page = time.time()

    async def alerts_poller(self):
        while True:
            try:
                alerts = await asyncio.to_thread(fetch_alerts, self.cfg)
                if ([a["type"] for a in alerts]
                        != [a["type"] for a in self.alerts]):
                    kinds = ", ".join(a["type"] for a in alerts) or "clear"
                    print(f"[{time.strftime('%H:%M:%S')}] service status: "
                          f"{kinds}")
                self.alerts = alerts
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] alerts fetch error: "
                      f"{e}", file=sys.stderr)
            await asyncio.sleep(ALERTS_POLL_SECS)

    def _fetch_active(self):
        """Arrivals for the first group with anything to show — the
        primary normally, the fallback (NV at Vairo Village) on days the
        primary routes rest."""
        first_status = None
        for g in self.groups:
            arrivals, status = fetch_arrivals(g, self.schedule)
            if first_status is None:
                first_status = status
            if arrivals:
                return g, arrivals, status
        return self.groups[0], [], first_status or {"held": {},
                                                    "occupancy": {}}

    async def fetcher(self):
        while True:
            try:
                shown = self.displayed()
                group, arrivals, status = await asyncio.to_thread(
                    self._fetch_active)
                if group is not self.cfg:
                    print(f"[{time.strftime('%H:%M:%S')}] switching to "
                          f"the {'/'.join(group['designators'])} group "
                          f"({group['dir_word']})")
                    self.cfg = group
                self.arrivals = arrivals
                self.held = status["held"]
                self.occupancy = status["occupancy"]
                match = shown and self._same_train(shown, self.arrivals)
                if time.time() < self.page_hold_until:
                    if match is not None:
                        self.index = match  # data stays fresh; page stays up
                elif shown and match is None and self.arrivals:
                    await self.departure_flash(shown[1])
                else:
                    if match is not None:
                        self.index = match
                    await self.render()
                nxt = ", ".join(
                    f"{designator(r)}:{int(max(0, t - time.time()) // 60)}m"
                    f"{'' if live else '*'}"
                    for t, r, _trip, _d, live, _l in self.arrivals[:4])
                state = "blocked" if self.blocked else "showing"
                print(f"[{time.strftime('%H:%M:%S')}] {len(self.arrivals)} "
                      f"arrivals ({state})  {nxt}")
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] fetch error: {e}",
                      file=sys.stderr)
            await asyncio.sleep(FETCH_SECS)

    async def supervisor(self):
        """Retry while blocked; flip the minutes exactly on the boundary;
        interrupt with the alert page on its cadence."""
        while True:
            await asyncio.sleep(BLOCKED_RETRY_SECS if self.blocked
                                else TICK_SECS)
            d = self.displayed()
            if d is None:
                continue
            if self.blocked:
                await self.render()  # cheap 409 until we own the screen
                continue
            if time.time() < self.page_hold_until:
                continue  # an alert page owns the screen right now
            if (self.status_assets and self.arrivals
                    and (self.canvas_mode or "").startswith("card")
                    and time.time() - self.last_alert_page > ALERT_PAGE_EVERY
                    and self._pick_page()):
                await self.alert_page()
                continue
            mins_now = int(max(0, d[0] - time.time()) // 60)
            if self.shown_key != (d[0], d[1], mins_now):
                await self.render()

    async def idle_reset(self):
        while True:
            await asyncio.sleep(5)
            if (self.index != 0 and not self.blocked
                    and time.time() >= self.page_hold_until
                    and time.time() - self.last_dial > IDLE_RESET_SECS):
                await self.slide_to(0, direction=-1)

    async def dial_listener(self):
        try:
            import websockets
        except ImportError:
            print("dial disabled: `pip install websockets` to scroll "
                  "arrivals with the dial over USB")
            return
        while True:
            try:
                async with websockets.connect(self.bar.t.ws_uri) as ws:
                    await ws.send(json.dumps({"enable": True}))
                    print("dial connected — spin to scroll arrivals")
                    async for msg in ws:
                        if isinstance(msg, str):
                            continue
                        moved = sum(encoder_deltas(msg))
                        if moved and self.arrivals:
                            self.last_dial = time.time()
                            await self.slide_to(
                                (self.index + moved) % len(self.arrivals),
                                direction=1 if moved > 0 else -1)
            except Exception as e:
                print(f"dial stream lost ({e}); retrying in 5s",
                      file=sys.stderr)
                await asyncio.sleep(5)

    async def run(self):
        # drop any elements a previous version left behind (draws merge by
        # id, so an element we no longer push would linger forever)
        try:
            await asyncio.to_thread(self.bar.clear)
        except requests.RequestException:
            pass
        tasks = [self.fetcher(), self.supervisor()]
        if self.status_assets:
            tasks.append(self.alerts_poller())
        if self.bar.t.ws_uri:
            tasks += [self.dial_listener(), self.idle_reset()]
        await asyncio.gather(*tasks)

    async def demo(self):
        """Fake departure sequence to preview the art and animations."""
        now = time.time()
        routes = self.cfg["route_ids"]
        self.arrivals = [
            (now + 30, routes[0], "demo-1", 0, True, False),
            (now + 360, routes[1 % len(routes)], "demo-2", 90, True, False),
            (now + 780, routes[0], "demo-3", 0, False, True),
        ]
        await self.render()
        await asyncio.sleep(2)
        departed = self.arrivals.pop(0)
        await self.departure_flash(departed[1])
        await asyncio.sleep(2)
        # the LAST tag, on the scheduled final departure
        self.index = len(self.arrivals) - 1
        await self.render()
        await asyncio.sleep(2)

    async def demo_alerts(self):
        """Stage every service-status screen with fake data, in sequence:
        card with alert dot, the alert-page cycle, DELAYED plate, the
        DETOUR page, NO BUSES plate, PLANNED plate, the quiet next-service
        plate. The [demo] markers let capture tooling slice states
        deterministically."""
        def mark(name):
            print(f"[demo] {name}", flush=True)

        now = time.time()
        routes = self.cfg["route_ids"]
        v = designator(routes[0])
        self.arrivals = [
            (now + 420, routes[0], "demo-1", 0, True, False),
            (now + 900, routes[1 % len(routes)], "demo-2", 0, True, False),
            (now + 1260, routes[0], "demo-3", 0, False, False),
        ]
        self.alerts = [{
            "kind": "delays", "type": "delays",
            "head": f"{v} buses are running with delays due to game day "
                    "traffic on North Atherton",
            "period": "", "routes": [v]}]
        mark("card_dot")
        await self.render()
        await asyncio.sleep(4)
        mark("alert_cycle")
        await self.alert_page()
        await asyncio.sleep(2)
        d = self.displayed()
        self.alerts = []
        self.held = {d[2]: (7 * 60, self.cfg["stops"][0])}
        mark("held")
        await self.render()
        await asyncio.sleep(6)
        self.held = {}
        self.arrivals[0] = (d[0], d[1], d[2], 6 * 60, True, False)
        mark("delayed")
        await self.render()
        await asyncio.sleep(6)
        self.arrivals[0] = d
        self.alerts = [{
            "kind": "detour", "type": "detour",
            "head": f"{v} buses are on detour via Martin St; the Vairo "
                    "Blvd at Tremont stop is closed",
            "period": "", "routes": [v]}]
        self.last_alert_page = 0
        mark("detour_cycle")
        await self.alert_page()
        await asyncio.sleep(1)
        self.arrivals = []
        self.alerts = [{
            "kind": "suspension", "type": "suspension",
            "head": f"No {v} service between Vairo Blvd and campus due to "
                    "a water main break on Oakwood Ave",
            "period": "", "routes": [v]}]
        mark("nobuses")
        await self.render()
        await asyncio.sleep(7)
        self.alerts = [{
            "kind": "planned", "type": "planned",
            "head": "Starting Monday, PennDOT construction on Vairo Blvd "
                    "will relocate the Vairo Village stops",
            "period": "", "routes": [v]}]
        mark("planned")
        await self.render()
        await asyncio.sleep(7)
        self.alerts = []
        mark("quiet")
        await self.render()
        await asyncio.sleep(4)
        mark("end")
        await asyncio.to_thread(self.bar.clear)

    async def preview_once(self):
        """One live fetch rendered to the preview dir: the board exactly
        as the Bar would show it right now."""
        try:
            self.alerts = await asyncio.to_thread(fetch_alerts,
                                                  self.full_cfg)
        except Exception as e:
            print(f"alerts fetch failed: {e}", file=sys.stderr)
        group, arrivals, status = await asyncio.to_thread(
            self._fetch_active)
        self.cfg = group
        self.arrivals = arrivals
        self.held = status["held"]
        self.occupancy = status["occupancy"]
        await self.render()
        nxt = ", ".join(
            f"{designator(r)}:{int(max(0, t - time.time()) // 60)}m"
            f"{'' if live else '*'}"
            for t, r, _trip, _d, live, _l in self.arrivals[:8])
        print(f"{len(self.arrivals)} arrivals "
              f"({'/'.join(group['designators'])}, {group['dir_word']})  "
              f"{nxt}   (* = scheduled, no realtime yet)")


def encoder_deltas(frame):
    """Extract dial rotation deltas from one status WS binary frame
    (BSB_State.State: updates=2 -> input=11 -> encoder_event=3 ->
    delta=1, zigzag sint32)."""
    deltas = []
    for f, w, update in _walk_fields(frame):
        if f != 2 or w != 2:
            continue
        for f2, w2, inp in _walk_fields(update):
            if f2 != 11 or w2 != 2:
                continue
            for f3, w3, enc in _walk_fields(inp):
                if f3 != 3 or w3 != 2:
                    continue
                delta = 0
                for f4, w4, v in _walk_fields(enc):
                    if f4 == 1 and w4 == 0:
                        delta = (v >> 1) ^ -(v & 1)  # zigzag decode
                deltas.append(delta)
    return deltas


# ----------------------------------------------------------------------- CLI

def config_error_loop(bar, err):
    """Config problems stay visible: log the details, show a short hint on
    the display, and keep the process alive (a crash-looping manager app is
    harder to diagnose from the dashboard than a message)."""
    print(err.message, file=sys.stderr)
    while True:
        try:
            bar.draw([{
                "id": "msg", "type": "text", "text": err.display_hint,
                "font": "small", "color": WHITE, "align": "center",
                "x": 36, "y": 8, "width": 72, "scroll_rate": 1500,
                "timeout": ELEMENT_TIMEOUT,
            }])
        except requests.RequestException:
            pass
        time.sleep(60)
        print(f"still misconfigured: {err.message}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Live CATA bus departures (State College, PA) on a "
                    "BUSY Bar")
    parser.add_argument("--host", default=None,
                        help="bar or manager-proxy host[:port] "
                             "(busybar-manager passes this)")
    parser.add_argument("--routes", default=None,
                        help="route short names, e.g. V,VE (env ROUTES)")
    parser.add_argument("--stops", default=None,
                        help="GTFS stop ids, e.g. 504,506 (env STOPS)")
    parser.add_argument("--direction", default=None,
                        help="campus/inbound/1 or outbound/0 "
                             "(env DIRECTION)")
    parser.add_argument("--list-stops", nargs="?", const="", default=None,
                        metavar="QUERY",
                        help="print matching stops with served routes and "
                             "exit")
    parser.add_argument("--clear", action="store_true",
                        help="clear display and exit")
    parser.add_argument("--demo", action="store_true",
                        help="run the departure-flash demo and exit")
    parser.add_argument("--demo-alerts", action="store_true",
                        help="stage every service-status screen with fake "
                             "data and exit")
    parser.add_argument("--preview", action="store_true",
                        help="no Bar needed: composite draws to PNGs "
                             "(combine with --demo/--demo-alerts, or "
                             "alone for one live board frame)")
    parser.add_argument("--preview-dir", default="preview",
                        help="output dir for --preview (default ./preview)")
    args = parser.parse_args()

    global requests
    try:
        import requests as _requests
    except ImportError:
        raise SystemExit("this app needs the `requests` package "
                         "(pip install requests)")
    requests = _requests

    if args.list_stops is not None:
        list_stops(load_schedule(), args.list_stops)
        return

    if args.preview:
        bar = PreviewBar(args.preview_dir)
    else:
        bar = Bar()
        bar.connect(args.host)

    if args.clear:
        bar.clear()
        print("cleared")
        return

    schedule = load_schedule()
    routes_csv = args.routes or os.environ.get("ROUTES") or ""
    stops_csv = args.stops or os.environ.get("STOPS") or ""
    direction = args.direction or os.environ.get("DIRECTION") or ""
    if not routes_csv and not stops_csv:
        print("no ROUTES/STOPS configured — defaulting to campus-bound "
              "V,VE at Vairo Blvd/Oakwood Ave (stops 504,506), NV fallback")
    try:
        cfg = resolve_config(
            schedule, routes_csv, stops_csv, direction,
            os.environ.get("FALLBACK_ROUTES") or "",
            os.environ.get("FALLBACK_STOPS") or "",
            os.environ.get("FALLBACK_DIRECTION") or "")
    except ConfigError as e:
        config_error_loop(bar, e)
        return

    for which, g in zip(("watching", "fallback"), cfg["groups"]):
        print(f"{which}: {g['label']} ({g['dir_word']}) — routes "
              f"{','.join(g['designators'])} — stops "
              f"{','.join(g['stops'])}")

    desigs = []
    for g in cfg["groups"]:
        for d in g["designators"]:
            if d not in desigs:
                desigs.append(d)
    assets = build_assets(desigs)
    status_assets = build_status_assets() if ALERTS_ON else {}
    try:
        bar.upload_assets(assets, status_assets)
    except requests.RequestException as e:
        print(f"asset upload failed: {e}", file=sys.stderr)

    app = App(bar, cfg, assets, status_assets, schedule)
    try:
        if args.demo_alerts:
            coro = app.demo_alerts()
        elif args.demo:
            coro = app.demo()
        elif args.preview:
            coro = app.preview_once()
        else:
            coro = app.run()
        asyncio.run(coro)
    except KeyboardInterrupt:
        try:
            bar.clear()
        except requests.RequestException:
            pass


if __name__ == "__main__":
    main()

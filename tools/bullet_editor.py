#!/usr/bin/env python3
"""Visual alignment editor for the route-bullet letters.

Serves a local page that renders every route bullet (15x15 disks, express
diamonds) and departure-flash card (72x16) through the REAL app.py art
pipeline, with an LED-dot preview. Arrow keys nudge the letter inside the
selected icon; Save bakes the offsets into app.py's generated OFFSETS
block; Preview pushes the selected icon to the physical Bar for a few
seconds (priority 50, so it briefly covers the running app); Deploy commits,
pushes to GitHub and refreshes the watchtower library install.

    python3 tools/bullet_editor.py             # http://localhost:8765
    python3 tools/bullet_editor.py --port 9000 --no-open

Run it with a python that has `requests` (only needed for Preview/Deploy),
e.g. ~/busybar/app/.venv/bin/python.
"""
import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "apps" / "catabus" / "app.py"
WATCHTOWER = "http://watchtower/api/_manager"
MAX_NUDGE = 6
PREVIEW_SECS = 6

# the display order of the icon grid (all designators + express variants)
ICON_ORDER = ["V", "VE", "NV"]


def load_env():
    """BUSYBAR_CLOUD_TOKEN for the preview target, from ~/busybar/.env."""
    import os
    env = Path.home() / "busybar" / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


load_env()
spec = importlib.util.spec_from_file_location("catabus_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

RENDER_LOCK = threading.Lock()
BAR_LOCK = threading.Lock()
BAR = None


SIZES = ("tiny", "bold", "xl")


def render_png(kind, route, dx, dy, size=""):
    """One icon through the real pipeline at candidate offset + size."""
    with RENDER_LOCK:
        saved_off, saved_sz = app.LETTER_OFFSETS, app.LETTER_SIZES
        eff_off = {k: dict(v) for k, v in saved_off.items()}
        eff_off.setdefault(kind, {})[route] = (dx, dy)
        eff_sz = {k: dict(v) for k, v in saved_sz.items()}
        if size:
            eff_sz.setdefault(kind, {})[route] = size
        else:
            eff_sz.get(kind, {}).pop(route, None)
        app.LETTER_OFFSETS, app.LETTER_SIZES = eff_off, eff_sz
        try:
            if kind == "bullet":
                return app.make_bullet(route)
            rows = app.flash_card(route)
            return app.png_encode(
                72, 16, [[(*px, 255) for px in row] for row in rows])
        finally:
            app.LETTER_OFFSETS, app.LETTER_SIZES = saved_off, saved_sz


def current_state():
    return ({kind: {r: list(v) for r, v in table.items()}
             for kind, table in app.LETTER_OFFSETS.items()},
            {kind: dict(table) for kind, table in app.LETTER_SIZES.items()})


def clean_payload(payload):
    """Validate + normalize the client's offsets and sizes dicts."""
    offs = {"bullet": {}, "flash": {}}
    for kind in offs:
        for route, pair in ((payload.get("offsets") or {}).get(kind)
                            or {}).items():
            if route not in ICON_ORDER:
                raise ValueError(f"unknown route {route!r}")
            dx, dy = int(pair[0]), int(pair[1])
            if abs(dx) > MAX_NUDGE or abs(dy) > MAX_NUDGE:
                raise ValueError(f"offset out of range for {route}")
            if (dx, dy) != (0, 0):
                offs[kind][route] = (dx, dy)
    sizes = {"bullet": {}, "flash": {}}
    for kind in sizes:
        for route, size in ((payload.get("sizes") or {}).get(kind)
                            or {}).items():
            if route not in ICON_ORDER or size not in SIZES:
                raise ValueError(f"bad size {size!r} for {route!r}")
            if size != app.default_size(kind, route):
                sizes[kind][route] = size
    return offs, sizes


def tuning_block(offsets, sizes):
    lines = ["# --- BEGIN GENERATED: OFFSETS ---",
             "# per-icon letter tuning, edited with tools/bullet_editor.py.",
             "# LETTER_OFFSETS: (dx, dy) nudges from dead center — the Q/G "
             "seeds carry",
             '# the legacy hand alignment. LETTER_SIZES: glyph size '
             "override per icon",
             '# ("tiny" ~3x4 / "bold" 7px / "xl" 10px); defaults are '
             "bullet=bold,",
             "# flash=xl for locals and bold inside the express diamond "
             "mark."]
    for name, table, fmt in (
            ("LETTER_OFFSETS", offsets, lambda v: f"({v[0]}, {v[1]})"),
            ("LETTER_SIZES", sizes, lambda v: f'"{v}"')):
        lines.append(f"{name} = {{")
        for kind in ("bullet", "flash"):
            entries = table.get(kind, {})
            if not entries:
                lines.append(f'    "{kind}": {{}},')
                continue
            lines.append(f'    "{kind}": {{')
            for route in sorted(entries):
                lines.append(f'        "{route}": {fmt(entries[route])},')
            lines.append("    },")
        lines.append("}")
    lines.append("# --- END GENERATED: OFFSETS ---")
    return "\n".join(lines)


def save_tuning(offsets, sizes):
    src = APP_PATH.read_text()
    pat = re.compile(r"# --- BEGIN GENERATED: OFFSETS ---.*?"
                     r"# --- END GENERATED: OFFSETS ---", re.S)
    if not pat.search(src):
        raise RuntimeError("OFFSETS marker not found in app.py")
    APP_PATH.write_text(pat.sub(lambda _: tuning_block(offsets, sizes), src))
    app.LETTER_OFFSETS = {k: dict(v) for k, v in offsets.items()}
    app.LETTER_SIZES = {k: dict(v) for k, v in sizes.items()}


def get_bar():
    global BAR
    if BAR is None:
        import requests
        app.requests = requests   # app.py imports it lazily in main()
        app.APP_NAME = "bullet-editor"
        app.PRIORITY = 50         # over the subway app (30), under BUSY (90)
        bar = app.Bar()
        bar.connect()
        BAR = bar
    return BAR


def preview_on_bar(kind, route, dx, dy, size=""):
    import requests
    png = render_png(kind, route, dx, dy, size)
    name = f"ed_{kind}_{route}_{hashlib.sha256(png).hexdigest()[:8]}.png"
    with BAR_LOCK:
        bar = get_bar()
        r = bar.s.post(bar.t.url("/assets/upload"),
                       params={"application_name": app.APP_NAME,
                               "file": name},
                       headers={**bar.t.headers,
                                "Content-Type": "application/octet-stream"},
                       data=png, timeout=20)
        r.raise_for_status()
        x = 28 if kind == "bullet" else 0
        drawn = bar.draw([{
            "id": "editor_prev", "type": "image", "path": name,
            "x": x, "y": 0, "timeout": PREVIEW_SECS,
        }])
    if not drawn:
        return ("Bar is busy — something higher-priority owns the screen "
                "(BUSY session / calendar). Try again when it's idle.")
    return (f"drawn via {bar.t.name} — on the Bar for ~{PREVIEW_SECS}s "
            f"(the subway app takes back over automatically)")


def run(cmd, cwd=ROOT):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip()
    return p.returncode, out


def deploy(offsets, sizes):
    """Bake, verify, commit, push, refresh the watchtower install."""
    import requests
    log = []
    save_tuning(offsets, sizes)
    log.append("✓ letter tuning baked into app.py")

    rc, out = run([sys.executable, str(ROOT / "tools" / "parity_check.py")])
    if rc:
        raise RuntimeError("parity check FAILED:\n" + out)
    log.append("✓ parity check passed")

    run(["git", "add", "-A"])
    rc, _ = run(["git", "diff", "--cached", "--quiet"])
    if rc:  # something staged
        msg = ("Tune bullet letter alignment in the editor\n\n"
               "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>")
        rc, out = run(["git", "commit", "-m", msg])
        if rc:
            raise RuntimeError("git commit failed:\n" + out)
        log.append("✓ committed alignment changes")
    else:
        log.append("· nothing new to commit")

    rc, out = run(["git", "push", "origin", "main"])
    if rc:
        raise RuntimeError("git push failed:\n" + out)
    log.append("✓ pushed to GitHub")

    try:
        # the library update endpoint no-ops until a fresh check
        r = requests.post(f"{WATCHTOWER}/library/check", timeout=60)
        log.append(f"· library check: {r.status_code} {r.text[:120].strip()}")
        r = requests.post(f"{WATCHTOWER}/library/update",
                          json={"slug": "catabus"}, timeout=120)
        log.append(f"✓ library update: {r.status_code} {r.text[:120].strip()}")
        r = requests.post(f"{WATCHTOWER}/apps/catabus/restart", timeout=60)
        log.append(f"✓ app restart: {r.status_code} {r.text[:120].strip()}")
    except requests.RequestException as e:
        log.append(f"! watchtower unreachable ({e}) — push landed; update "
                   "from the dashboard when you're on the tailnet")
    return "\n".join(log)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/icons":
            offs, szs = current_state()
            icons = []
            for kind in ("bullet", "flash"):
                for route in ICON_ORDER:
                    dx, dy = offs.get(kind, {}).get(route, (0, 0))
                    icons.append({
                        "kind": kind, "route": route,
                        "w": 15 if kind == "bullet" else 72,
                        "h": 15 if kind == "bullet" else 16,
                        "dx": dx, "dy": dy,
                        "size": szs.get(kind, {}).get(route, ""),
                        "defaultSize": app.default_size(kind, route),
                        "express": app.is_express(route),
                        "color": app.line_color(route),
                    })
            return self._json({"icons": icons, "maxNudge": MAX_NUDGE,
                               "sizes": list(SIZES)})
        if url.path == "/render":
            q = parse_qs(url.query)
            try:
                kind = q["kind"][0]
                route = q["route"][0]
                dx = int(q.get("dx", ["0"])[0])
                dy = int(q.get("dy", ["0"])[0])
                size = q.get("size", [""])[0]
                if (kind not in ("bullet", "flash")
                        or route not in ICON_ORDER
                        or size not in ("",) + SIZES):
                    raise ValueError
            except (KeyError, ValueError):
                return self._json({"error": "bad render request"}, 400)
            return self._send(200, render_png(kind, route, dx, dy, size),
                              "image/png")
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        try:
            if self.path == "/save":
                save_tuning(*clean_payload(payload))
                return self._json({"ok": True,
                                   "message": "letter tuning baked into "
                                              "app.py"})
            if self.path == "/preview":
                msg = preview_on_bar(
                    payload["kind"], payload["route"],
                    int(payload.get("dx", 0)), int(payload.get("dy", 0)),
                    payload.get("size", ""))
                return self._json({"ok": True, "message": msg})
            if self.path == "/deploy":
                log = deploy(*clean_payload(payload))
                return self._json({"ok": True, "message": log})
        except Exception as e:  # surfaced in the UI, keep serving
            global BAR
            BAR = None  # a dead connection shouldn't wedge later previews
            return self._json({"ok": False, "message": str(e)}, 500)
        return self._json({"error": "not found"}, 404)


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>BUSY Bar bullet editor</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0c0c0e; color: #e8e8ea;
         font: 14px/1.45 ui-sans-serif, system-ui, sans-serif; }
  header { position: sticky; top: 0; z-index: 5; display: flex; gap: 12px;
           align-items: center; padding: 10px 18px; background: #121215;
           border-bottom: 1px solid #232328; }
  header h1 { font-size: 15px; margin: 0 8px 0 0; font-weight: 650; }
  .state { font-size: 12px; color: #9a9aa2; margin-right: auto; }
  .state.dirty { color: #ffb64f; }
  button { background: #1d1d22; color: #e8e8ea; border: 1px solid #303038;
           border-radius: 7px; padding: 7px 13px; font: inherit;
           cursor: pointer; }
  button:hover { background: #26262c; }
  button.primary { background: #2450b8; border-color: #2e62dd; }
  button.primary:hover { background: #2a5bd0; }
  button.deploy { background: #14602c; border-color: #1d7c3b; }
  button.deploy:hover { background: #187234; }
  main { display: grid; grid-template-columns: 1fr 560px; gap: 0;
         align-items: start; }
  #grids { padding: 16px 18px 80px; min-width: 0; }
  h2 { font-size: 12px; letter-spacing: .12em; text-transform: uppercase;
       color: #8f8f98; margin: 22px 0 10px; }
  .grid { display: flex; flex-wrap: wrap; gap: 10px; }
  .tile { display: flex; flex-direction: column; align-items: center;
          gap: 5px; padding: 9px 9px 7px; background: #131316;
          border: 1px solid #232328; border-radius: 10px; cursor: pointer;
          position: relative; }
  .tile:hover { border-color: #3a3a44; }
  .tile.sel { border-color: #4f8dff; box-shadow: 0 0 0 1px #4f8dff; }
  .tile .name { font-size: 12px; color: #b7b7bf; }
  .tile .off { position: absolute; top: 5px; right: 7px; font-size: 10px;
               color: #ffb64f; }
  aside { position: sticky; top: 49px; height: calc(100vh - 49px);
          overflow: auto; padding: 18px; background: #101013;
          border-left: 1px solid #232328; }
  #bigwrap { background: #0a0a0b; border: 1px solid #232328;
             border-radius: 10px; padding: 14px; display: flex;
             justify-content: center; }
  #big { image-rendering: pixelated; max-width: 100%; }
  .row { display: flex; gap: 10px; align-items: center; margin-top: 14px;
         flex-wrap: wrap; }
  .pad { display: grid; grid-template-columns: repeat(3, 44px);
         grid-auto-rows: 40px; gap: 6px; }
  .pad button { font-size: 16px; padding: 0; }
  .pad .blank { visibility: hidden; }
  #meta { font-size: 13px; color: #b7b7bf; }
  #meta b { color: #fff; font-size: 15px; }
  #offread { font-variant-numeric: tabular-nums; font-size: 20px;
             font-weight: 650; }
  #msg { margin-top: 12px; font-size: 12.5px; color: #9a9aa2;
         white-space: pre-wrap; }
  #msg.err { color: #ff7a6e; }
  #msg.ok { color: #6fd487; }
  .hint { margin-top: 18px; font-size: 12px; color: #6f6f78;
          line-height: 1.7; }
  kbd { background: #1d1d22; border: 1px solid #303038; border-radius: 4px;
        padding: 1px 5px; font-size: 11px; }
  #sizeRow button.on { border-color: #4f8dff; box-shadow: 0 0 0 1px #4f8dff; }
  #sizeRow button.def::after { content: " ·"; color: #8f8f98; }
  @media (max-width: 1100px) { main { grid-template-columns: 1fr; }
    aside { position: static; height: auto; border-left: 0;
            border-top: 1px solid #232328; } }
</style>
<header>
  <h1>BUSY Bar bullet editor</h1>
  <span class="state" id="state">loading…</span>
  <button id="mode">LED dots</button>
  <button id="previewBtn" title="Push the selected icon to the physical Bar
for a few seconds">Preview on Bar</button>
  <button id="saveBtn" class="primary">Save to app.py</button>
  <button id="deployBtn" class="deploy">Save &amp; Deploy</button>
</header>
<main>
  <div id="grids">
    <h2>Route bullets — 15×15 (diamonds = rush-hour express)</h2>
    <div class="grid" id="bullets"></div>
    <h2>Departure flash — 72×16 full-screen</h2>
    <div class="grid" id="flashes"></div>
  </div>
  <aside>
    <div id="bigwrap"><canvas id="big"></canvas></div>
    <div class="row">
      <div id="meta"></div>
      <div id="offread" style="margin-left:auto"></div>
    </div>
    <div class="row">
      <div class="pad">
        <span class="blank"></span>
        <button data-n="0,-1">▲</button>
        <span class="blank"></span>
        <button data-n="-1,0">◀</button>
        <button id="resetBtn" title="Back to (0,0) and default size">·</button>
        <button data-n="1,0">▶</button>
        <span class="blank"></span>
        <button data-n="0,1">▼</button>
        <span class="blank"></span>
      </div>
      <div class="hint" style="margin:0">
        <kbd>←→↑↓</kbd> nudge the letter<br>
        <kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd> letter size tiny / bold / XL<br>
        <kbd>[</kbd> <kbd>]</kbd> previous / next icon<br>
        <kbd>r</kbd> reset · <kbd>s</kbd> save · <kbd>p</kbd> preview on Bar
      </div>
    </div>
    <div class="row" id="sizeRow">
      <span style="font-size:12px;color:#8f8f98">Letter size</span>
      <button data-s="tiny">tiny</button>
      <button data-s="bold">bold</button>
      <button data-s="xl">XL</button>
    </div>
    <div id="msg"></div>
    <div class="hint">Nudges and sizes change only the letter; disks,
    diamonds and shadows re-render around it. Sizes come from the Bar's own
    font family (tiny ~3×4, bold 7 px, XL 10 px) — pixel-authentic, never
    rescaled; the dot marks each icon's default. Save bakes everything into
    app.py's OFFSETS block (safe to run repeatedly). Save &amp; Deploy also
    commits, pushes to GitHub and refreshes the watchtower install, so the
    Bar picks the art up. Nudges are clamped to ±<span id="maxn"></span>
    px.</div>
  </aside>
</main>
<script>
const $ = s => document.querySelector(s);
let icons = [], sel = null, maxN = 6, led = true;
const offsets = { bullet: {}, flash: {} };
const sizes = { bullet: {}, flash: {} };
let savedJSON = "";

const key = ic => ic.kind + "/" + ic.route;
const getOff = ic => offsets[ic.kind][ic.route] || [0, 0];
const setOff = (ic, dx, dy) => {
  dx = Math.max(-maxN, Math.min(maxN, dx));
  dy = Math.max(-maxN, Math.min(maxN, dy));
  if (dx === 0 && dy === 0) delete offsets[ic.kind][ic.route];
  else offsets[ic.kind][ic.route] = [dx, dy];
};
const getSize = ic => sizes[ic.kind][ic.route] || ic.defaultSize;
const setSize = (ic, s) => {
  if (s === ic.defaultSize) delete sizes[ic.kind][ic.route];
  else sizes[ic.kind][ic.route] = s;
};
const stateJSON = () => JSON.stringify({ offsets, sizes });
const dirty = () => stateJSON() !== savedJSON;

function setState() {
  const st = $("#state");
  st.textContent = dirty() ? "unsaved changes" : "saved";
  st.className = "state" + (dirty() ? " dirty" : "");
}

async function fetchImage(ic) {
  const [dx, dy] = getOff(ic);
  const sz = sizes[ic.kind][ic.route] || "";
  const r = await fetch(`/render?kind=${ic.kind}&route=${ic.route}` +
                        `&dx=${dx}&dy=${dy}&size=${sz}`);
  return createImageBitmap(await r.blob());
}

function drawIcon(canvas, bmp, w, h, s) {
  canvas.width = w * s; canvas.height = h * s;
  const ctx = canvas.getContext("2d");
  const off = new OffscreenCanvas(w, h);
  const octx = off.getContext("2d");
  octx.drawImage(bmp, 0, 0);
  const d = octx.getImageData(0, 0, w, h).data;
  ctx.fillStyle = "#0a0a0b";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = (y * w + x) * 4;
    const lit = d[i + 3] > 0 && (d[i] + d[i + 1] + d[i + 2]) > 0;
    const col = lit ? `rgb(${d[i]},${d[i+1]},${d[i+2]})` : "#151517";
    ctx.fillStyle = col;
    if (led) {
      ctx.beginPath();
      ctx.arc((x + .5) * s, (y + .5) * s, s * .37, 0, 6.2832);
      ctx.fill();
    } else {
      ctx.fillRect(x * s + .5, y * s + .5, s - 1, s - 1);
    }
  }
}

const tokens = {};
async function refresh(ic) {
  const t = (tokens[key(ic)] = (tokens[key(ic)] || 0) + 1);
  const bmp = await fetchImage(ic);
  if (tokens[key(ic)] !== t) return;      // a newer nudge superseded us
  const s = ic.kind === "bullet" ? 8 : 4;
  drawIcon(ic._canvas, bmp, ic.w, ic.h, s);
  const [dx, dy] = getOff(ic);
  ic._off.textContent = (dx || dy) ? `${dx>=0?"+":""}${dx},${dy>=0?"+":""}${dy}` : "";
  if (sel === ic) {
    const bs = ic.kind === "bullet" ? 24 : 7.2;
    drawIcon($("#big"), bmp, ic.w, ic.h, bs);
    const [sdx, sdy] = getOff(ic);
    $("#meta").innerHTML = `<b>${ic.route}</b> ${ic.kind}` +
      (ic.express ? " · express diamond" : "") + ` · ${ic.w}×${ic.h}`;
    $("#offread").textContent =
      `${sdx>=0?"+":""}${sdx}, ${sdy>=0?"+":""}${sdy}`;
    document.querySelectorAll("#sizeRow button").forEach(b => {
      b.classList.toggle("on", b.dataset.s === getSize(ic));
      b.classList.toggle("def", b.dataset.s === ic.defaultSize);
    });
  }
  setState();
}

function select(ic) {
  sel = ic;
  icons.forEach(i => i._tile.classList.toggle("sel", i === ic));
  refresh(ic);
  ic._tile.scrollIntoView({ block: "nearest" });
}

function nudge(dx, dy) {
  if (!sel) return;
  const [cx, cy] = getOff(sel);
  setOff(sel, cx + dx, cy + dy);
  refresh(sel);
}

function message(text, cls) {
  $("#msg").textContent = text;
  $("#msg").className = cls || "";
}

async function post(path, body, busyText) {
  message(busyText || "working…");
  try {
    const r = await fetch(path, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    const j = await r.json();
    message(j.message || JSON.stringify(j), j.ok ? "ok" : "err");
    return j;
  } catch (e) { message(String(e), "err"); return { ok: false }; }
}

async function save() {
  const j = await post("/save", { offsets, sizes }, "saving…");
  if (j.ok) { savedJSON = stateJSON(); setState(); }
}

async function init() {
  const j = await (await fetch("/icons")).json();
  icons = j.icons; maxN = j.maxNudge;
  $("#maxn").textContent = maxN;
  for (const ic of icons) {
    if (ic.dx || ic.dy) offsets[ic.kind][ic.route] = [ic.dx, ic.dy];
    if (ic.size) sizes[ic.kind][ic.route] = ic.size;
  }
  savedJSON = stateJSON();
  for (const ic of icons) {
    const tile = document.createElement("div");
    tile.className = "tile";
    const c = document.createElement("canvas");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = ic.route + (ic.express ? " ◆" : "");
    const off = document.createElement("span");
    off.className = "off";
    tile.append(off, c, name);
    tile.onclick = () => select(ic);
    ic._tile = tile; ic._canvas = c; ic._off = off;
    $(ic.kind === "bullet" ? "#bullets" : "#flashes").append(tile);
    refresh(ic);
  }
  select(icons[0]);
  setState();
}

$("#saveBtn").onclick = save;
$("#resetBtn").onclick = () => {
  if (sel) { setOff(sel, 0, 0); setSize(sel, sel.defaultSize); refresh(sel); }
};
document.querySelectorAll("#sizeRow button").forEach(b => {
  b.onclick = () => { if (sel) { setSize(sel, b.dataset.s); refresh(sel); } };
});
$("#mode").onclick = () => {
  led = !led;
  $("#mode").textContent = led ? "LED dots" : "square pixels";
  icons.forEach(refresh);
};
$("#previewBtn").onclick = () => {
  if (!sel) return;
  const [dx, dy] = getOff(sel);
  post("/preview", { kind: sel.kind, route: sel.route, dx, dy,
                     size: sizes[sel.kind][sel.route] || "" },
       "pushing to the Bar…");
};
$("#deployBtn").onclick = async () => {
  if (!confirm("Bake the letter tuning, run the parity check, commit, " +
               "push to GitHub and refresh the watchtower install?")) return;
  const j = await post("/deploy", { offsets, sizes }, "deploying… (git " +
                       "push + watchtower update — can take ~30s)");
  if (j.ok) { savedJSON = stateJSON(); setState(); }
};
document.querySelectorAll(".pad button[data-n]").forEach(b => {
  const [dx, dy] = b.dataset.n.split(",").map(Number);
  b.onclick = () => nudge(dx, dy);
});
document.addEventListener("keydown", e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key;
  if (k === "ArrowLeft") { nudge(-1, 0); e.preventDefault(); }
  else if (k === "ArrowRight") { nudge(1, 0); e.preventDefault(); }
  else if (k === "ArrowUp") { nudge(0, -1); e.preventDefault(); }
  else if (k === "ArrowDown") { nudge(0, 1); e.preventDefault(); }
  else if (k === "]") select(icons[(icons.indexOf(sel) + 1) % icons.length]);
  else if (k === "[") select(icons[(icons.indexOf(sel) - 1 + icons.length)
                                   % icons.length]);
  else if (k === "1" || k === "2" || k === "3") {
    if (sel) { setSize(sel, ["tiny", "bold", "xl"][+k - 1]); refresh(sel); }
  }
  else if (k === "r") $("#resetBtn").onclick();
  else if (k === "s") save();
  else if (k === "p") $("#previewBtn").onclick();
});
window.addEventListener("beforeunload", e => {
  if (dirty()) e.preventDefault();
});
init();
</script>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"bullet editor at {url} — Ctrl-C to stop")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

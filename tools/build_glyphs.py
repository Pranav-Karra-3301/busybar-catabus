#!/usr/bin/env python3
"""Bake route-letter glyphs + legacy hand-tuned art into apps/nyc-subway/app.py.

Sources:
  - tools/data/busy_bold_7.font   -> BULLET_GLYPHS (letters/digits inside the
                                     15x15 route disks)
  - tools/data/busy_bold_10.font  -> XL_GLYPHS (the big letter riding the
                                     departure flash)
  - tools/data/busy_tiny.font     -> TINY_GLYPHS (the small size option in
                                     the bullet editor; chars the tiny font
                                     lacks simply don't get an entry)
  - tools/data/busy_condensed_7.font -> CONDENSED_GLYPHS (the narrow face
                                     the status plates bake their words in)
  - tools/data/busy_regular_5.font -> SMALL_GLYPHS (the status marquee face)
  - ~/busybar/app/canal_trains.py + g_trains.py -> BULLET_GLYPH_OVERRIDES
    (the hand-tuned N/Q/G letterforms, lifted as masks out of the legacy
    15x15 disk PNGs' black pixels) and the disk alpha mask every generated
    bullet is drawn on. Letterforms that match the font glyph (N) emit no
    override; hand-tuned POSITIONS ride in app.py's LETTER_OFFSETS block,
    which belongs to tools/bullet_editor.py and is not rewritten here.

The fonts are lv_font_conv "bin" format (head/cmap/loca/glyf); the busy_*
fonts ship in busy-app/busybar-firmware under OFL-1.1. The glyf bitstream per
glyph: advance, bbox x, bbox y, bbox w, bbox h, then w*h pixels of `bpp` bits,
row-major, MSB-first (compression 0 only).

Hand-tuned XL N/Q/G below were dumped off the device screen and user-tuned in
~/busybar (tools/build_trains_art.py) — they override the font-derived ones.

    python3 tools/build_glyphs.py --preview      # print glyphs to terminal
    python3 tools/build_glyphs.py --write        # rewrite app.py markers
"""
import argparse
import base64
import io
import re
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "catabus" / "app.py"
DATA = ROOT / "tools" / "data"
LEGACY = Path.home() / "busybar" / "app"

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# hand-tuned flash letters (dumped off the device screen, user-tuned;
# from ~/busybar/tools/build_trains_art.py XL_GLYPHS) — always win
XL_OVERRIDES = {
    "N": ["##...##", "##...##", "###..##", "####.##", "#######",
          "##.####", "##..###", "##...##", "##...##", "##...##"],
    "Q": [".#####.", "#######", "##...##", "##...##", "##...##",
          "##...##", "##.####", "##.###.", "#######", ".###.##"],
    "G": [".#####.", "#######", "##...##", "##.....", "##..###",
          "##..###", "##...##", "##...##", "#######", ".#####."],
}


class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, nbits: int) -> int:
        v = 0
        for _ in range(nbits):
            byte = self.data[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def read_signed(self, nbits: int) -> int:
        v = self.read(nbits)
        if v >= 1 << (nbits - 1):
            v -= 1 << nbits
        return v


def parse_tables(blob: bytes) -> dict[str, bytes]:
    tables, off = {}, 0
    while off + 8 <= len(blob):
        (size,) = struct.unpack_from("<I", blob, off)
        if size < 8:
            break
        label = blob[off + 4:off + 8].decode("ascii", "replace")
        tables[label] = blob[off + 8:off + size]
        off += size
    return tables


def font_glyphs(font_path: Path) -> dict[str, list[str]]:
    """char -> rows of '#'/'.' (ink box only), for CHARS present in the font."""
    tables = parse_tables(font_path.read_bytes())
    head, cmap, loca, glyf = (tables["head"], tables["cmap"],
                              tables["loca"], tables["glyf"])
    (version,) = struct.unpack_from("<I", head, 0)
    if version != 1:
        raise SystemExit(f"{font_path.name}: unsupported font version {version}")
    (index_to_loc, _glyph_id_fmt, adv_fmt, bpp, xy_bits, wh_bits, adv_bits,
     compression, _subpixels) = struct.unpack_from("<9B", head, 26)
    if compression != 0:
        raise SystemExit(f"{font_path.name}: compressed bitmaps not supported")

    (sub_count,) = struct.unpack_from("<I", cmap, 0)
    code_to_glyph: dict[int, int] = {}
    for i in range(sub_count):
        (data_off, start, rng_len, gid_off, entries, fmt, _pad) = \
            struct.unpack_from("<IIHHHBB", cmap, 4 + i * 16)
        base = data_off - 8
        if fmt == 0:
            for j in range(rng_len):
                code_to_glyph[start + j] = gid_off + cmap[base + j]
        elif fmt == 2:
            for j in range(rng_len):
                code_to_glyph[start + j] = gid_off + j
        elif fmt == 1:
            for j in range(entries):
                (cd,) = struct.unpack_from("<H", cmap, base + j * 2)
                (gid,) = struct.unpack_from("<H", cmap, base + entries * 2 + j * 2)
                code_to_glyph[start + cd] = gid_off + gid
        elif fmt == 3:
            for j in range(entries):
                (cd,) = struct.unpack_from("<H", cmap, base + j * 2)
                code_to_glyph[start + cd] = gid_off + j
        else:
            raise SystemExit(f"unknown cmap subtable format {fmt}")

    (glyph_count,) = struct.unpack_from("<I", loca, 0)
    fmt_s, step = ("<I", 4) if index_to_loc else ("<H", 2)
    offsets = [struct.unpack_from(fmt_s, loca, 4 + i * step)[0]
               for i in range(glyph_count)]

    on_threshold = (1 << bpp) // 2  # >= half-max reads as ink

    out: dict[str, list[str]] = {}
    for ch in CHARS:
        gid = code_to_glyph.get(ord(ch))
        if gid is None or gid >= glyph_count:
            continue
        br = BitReader(glyf)
        br.pos = (offsets[gid] - 8) * 8
        adv = br.read(adv_bits)
        if adv_fmt == 1:
            adv = round(adv / 16)
        _x = br.read_signed(xy_bits)
        _y = br.read_signed(xy_bits)
        w = br.read(wh_bits)
        h = br.read(wh_bits)
        if not (0 < w <= 16 and 0 < h <= 16):
            raise SystemExit(f"{font_path.name} {ch!r}: implausible box {w}x{h} "
                             "(glyf record layout mismatch?)")
        rows = []
        for _ in range(h):
            row = ""
            for _ in range(w):
                v = br.read(bpp) if bpp > 1 else br.read(1)
                row += "#" if v >= max(on_threshold, 1) else "."
            rows.append(row)
        out[ch] = rows
    return out


def trim(rows: list[str]) -> list[str]:
    """Crop to the ink bounding box (fonts may pad the bbox)."""
    ys = [i for i, r in enumerate(rows) if "#" in r]
    if not ys:
        return rows
    rows = rows[ys[0]:ys[-1] + 1]
    xs = [i for r in rows for i, c in enumerate(r) if c == "#"]
    x0, x1 = min(xs), max(xs)
    return [r[x0:x1 + 1] for r in rows]


def legacy_bullets() -> dict[str, bytes]:
    """The shipped hand-tuned 15x15 disk PNGs out of the two legacy apps."""
    srcs = {"N": "canal_trains.py", "Q": "canal_trains.py", "G": "g_trains.py"}
    out = {}
    for route, fname in srcs.items():
        text = (LEGACY / fname).read_text()
        m = re.search(rf'"{route}": base64\.b64decode\(\n((?:\s+"[^"]*"\n?)+)\)',
                      text)
        if not m:
            raise SystemExit(f"bullet_{route} block not found in {fname}")
        out[route] = base64.b64decode("".join(re.findall(r'"([^"]*)"', m.group(1))))
    return out


def disk_mask(png: bytes) -> list[str]:
    """15x15 '#'/'.' alpha mask of the legacy disk (the shape all generated
    bullets are drawn on)."""
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = img.size
    px = img.load()
    return ["".join("#" if px[x, y][3] else "." for x in range(w))
            for y in range(h)]


def letter_mask(png: bytes) -> tuple[list[str], tuple[int, int]]:
    """(rows, (x0, y0)) of a legacy bullet's letter — its pure-black pixels.
    Safe classifier: the shaded ramps never reach #000000 inside the disk."""
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = img.size
    px = img.load()
    ink = {(x, y) for y in range(h) for x in range(w)
           if px[x, y] == (0, 0, 0, 255)}
    if not ink:
        raise SystemExit("no letter pixels found in legacy bullet")
    x0, x1 = min(x for x, _ in ink), max(x for x, _ in ink)
    y0, y1 = min(y for _, y in ink), max(y for _, y in ink)
    rows = ["".join("#" if (x, y) in ink else "."
                    for x in range(x0, x1 + 1)) for y in range(y0, y1 + 1)]
    return rows, (x0, y0)


def glyph_dict_src(name: str, glyphs: dict[str, list[str]]) -> str:
    lines = [f"{name} = {{"]
    for ch in sorted(glyphs):
        rows = ", ".join(f'"{r}"' for r in glyphs[ch])
        lines.append(f'    "{ch}": [{rows}],')
    lines.append("}")
    return "\n".join(lines)


def replace_marker(src: str, name: str, body: str) -> str:
    begin = f"# --- BEGIN GENERATED: {name} ---"
    end = f"# --- END GENERATED: {name} ---"
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    if not pattern.search(src):
        raise SystemExit(f"marker {name} not found in {APP}")
    stamp = f"{begin}\n# generated by tools/build_glyphs.py — do not edit by hand\n{body}\n{end}"
    return pattern.sub(lambda _: stamp, src)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preview", action="store_true", help="print glyphs")
    ap.add_argument("--write", action="store_true", help="rewrite app.py")
    args = ap.parse_args()

    bullet = {ch: trim(rows)
              for ch, rows in font_glyphs(DATA / "busy_bold_7.font").items()}
    xl = {ch: trim(rows)
          for ch, rows in font_glyphs(DATA / "busy_bold_10.font").items()}
    xl.update(XL_OVERRIDES)
    tiny = {ch: trim(rows)
            for ch, rows in font_glyphs(DATA / "busy_tiny.font").items()}
    condensed = {ch: trim(rows)
                 for ch, rows in font_glyphs(
                     DATA / "busy_condensed_7.font").items()}
    small = {ch: trim(rows)
             for ch, rows in font_glyphs(
                 DATA / "busy_regular_5.font").items()}

    if args.preview:
        for label, table in (("BULLET (busy_bold_7)", bullet),
                             ("XL (busy_bold_10 + overrides)", xl)):
            print(f"== {label} ==")
            for ch in CHARS:
                if ch in table:
                    g = table[ch]
                    print(f"-- {ch} ({len(g[0])}x{len(g)})")
                    print("\n".join(g))
        return

    # The NYC repo lifted hand-tuned N/Q/G letterforms and the disk mask
    # out of the legacy canal/g apps. For catabus those apps are not a
    # dependency: reuse whatever DISK_MASK / BULLET_GLYPH_OVERRIDES app.py
    # already carries (they were vendored from the NYC pipeline), and only
    # extract fresh ones when the legacy files happen to be present.
    src = APP.read_text()
    if (LEGACY / "canal_trains.py").exists():
        overrides = legacy_bullets()
        mask = disk_mask(overrides["N"])
        glyph_overrides = {}
        for route, png in sorted(overrides.items()):
            rows, (x0, y0) = letter_mask(png)
            if rows != bullet.get(route):
                glyph_overrides[route] = rows
    else:
        m = re.search(r"DISK_MASK = \[(.*?)\]", src, re.S)
        mask = [x.strip(' "') for x in m.group(1).split(",") if x.strip()]
        m = re.search(
            r"BULLET_GLYPH_OVERRIDES = (\{.*?\n\})", src, re.S)
        glyph_overrides = eval(m.group(1)) if m else {}
        print("legacy canal/g apps not present — keeping the existing "
              "disk mask and letterform overrides")

    body = "\n".join([
        glyph_dict_src("BULLET_GLYPHS", bullet),
        glyph_dict_src("XL_GLYPHS", xl),
        glyph_dict_src("TINY_GLYPHS", tiny),
        glyph_dict_src("CONDENSED_GLYPHS", condensed),
        glyph_dict_src("SMALL_GLYPHS", small),
        glyph_dict_src("DISK_MASK_ROWS", {"@": mask}).replace(
            'DISK_MASK_ROWS = {\n    "@": [', "DISK_MASK = ["
        ).replace("],\n}", "]"),
        glyph_dict_src("BULLET_GLYPH_OVERRIDES", glyph_overrides),
    ])
    APP.write_text(replace_marker(src, "GLYPHS", body))
    n_b = len(bullet)
    print(f"wrote GLYPHS: {n_b} bullet glyphs, {len(xl)} XL glyphs "
          f"({len(XL_OVERRIDES)} overridden), {len(tiny)} tiny + "
          f"{len(condensed)} condensed + {len(small)} small glyphs, "
          f"disk mask 15x15, "
          f"{len(glyph_overrides)} hand-tuned letterforms")


if __name__ == "__main__":
    main()

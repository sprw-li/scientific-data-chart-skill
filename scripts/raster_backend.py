#!/usr/bin/env python3
"""raster_backend.py — scene -> deterministic PNG raster + vector PDF (contract §4).

    write_png(scene, path, fs, dpi) -> {"path","px","dpi","sha256",...}
    write_pdf(scene, path, fs)      -> {"path","pages","page_mm","fonts_embedded",...}

Raster engines, best first (never a hard dependency):
    1. Pillow — antialiased text, drawn at 2x and box-filtered to the exact
       requested pixel grid so the byte output is deterministic;
    2. matplotlib Agg — used only when Pillow is missing;
    3. a stdlib PNG writer (zlib) — shapes only, flagged in the result dict.

PDF is written by this module: a single page, MediaBox exactly the canvas in
points, every glyph drawn with a text operator (vector, selectable) and every
face embedded as a subsetted TrueType /FontFile2 in a Type0/Identity-H font so
CJK and Latin share one code path.  No timestamps, fixed object order.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import hashlib
import math
import os
import re
import struct
import zlib

MM_PER_IN = 25.4
MM_PT = 72.0 / MM_PER_IN

# --------------------------------------------------------------------------- #
# colours
# --------------------------------------------------------------------------- #
def hex_rgb(value, default=(0.0, 0.0, 0.0)):
    if not isinstance(value, str):
        return default
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return default
    try:
        return (int(v[0:2], 16) / 255.0, int(v[2:4], 16) / 255.0, int(v[4:6], 16) / 255.0)
    except ValueError:
        return default


def hex_rgb255(value, default=(0, 0, 0)):
    r, g, b = hex_rgb(value, (default[0] / 255.0, default[1] / 255.0, default[2] / 255.0))
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def _no_paint(style, key):
    v = style.get(key)
    return v is None or v == "none" or (isinstance(v, str) and v.strip() == "")


# --------------------------------------------------------------------------- #
# geometry helpers shared by the engines
# --------------------------------------------------------------------------- #
def _cubic_points(p0, c1, c2, p1, steps=24):
    out = []
    for i in range(1, steps):
        t = i / float(steps)
        mt = 1.0 - t
        x = (mt ** 3) * p0[0] + 3 * (mt ** 2) * t * c1[0] + 3 * mt * (t ** 2) * c2[0] + (t ** 3) * p1[0]
        y = (mt ** 3) * p0[1] + 3 * (mt ** 2) * t * c1[1] + 3 * mt * (t ** 2) * c2[1] + (t ** 3) * p1[1]
        out.append((x, y))
    return out


def _rect_path(x, y, w, h, rx, ry=None, steps=8):
    """Return a polygon approximation of a (rounded) rectangle."""
    if not rx or rx <= 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    ry = ry or rx
    rx = min(rx, w / 2.0)
    ry = min(ry, h / 2.0)
    pts = []

    def arc(cx, cy, a0, a1):
        for i in range(steps + 1):
            a = a0 + (a1 - a0) * i / float(steps)
            pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))

    arc(x + w - rx, y + ry, -math.pi / 2, 0)
    arc(x + w - rx, y + h - ry, 0, math.pi / 2)
    arc(x + rx, y + h - ry, math.pi / 2, math.pi)
    arc(x + rx, y + ry, math.pi, 1.5 * math.pi)
    return pts


def _ellipse_pts(cx, cy, rx, ry, steps=48):
    return [(cx + rx * math.cos(2 * math.pi * i / steps),
             cy + ry * math.sin(2 * math.pi * i / steps)) for i in range(steps)]


def _dash_segments(points, dash):
    """Split a polyline into dashed sub-segments (deterministic)."""
    if not dash:
        return [points]
    pattern = [float(v) for v in dash if float(v) > 0]
    if not pattern:
        return [points]
    out = []
    on, idx, remain = True, 0, pattern[0]
    cur = [points[0]]
    for i in range(1, len(points)):
        p0, p1 = points[i - 1], points[i]
        seg = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        travelled = 0.0
        while seg - travelled > 1e-9:
            step = min(remain, seg - travelled)
            f0, f1 = travelled / seg, (travelled + step) / seg
            a = (p0[0] + (p1[0] - p0[0]) * f0, p0[1] + (p1[1] - p0[1]) * f0)
            bb = (p0[0] + (p1[0] - p0[0]) * f1, p0[1] + (p1[1] - p0[1]) * f1)
            if on:
                if cur[-1] != a:
                    cur.append(a)
                cur.append(bb)
            else:
                if len(cur) > 1:
                    out.append(cur)
                cur = [bb]
            travelled += step
            remain -= step
            if remain <= 1e-9:
                idx = (idx + 1) % len(pattern)
                remain = pattern[idx]
                if not on and len(cur) > 1:
                    out.append(cur)
                    cur = [bb]
                on = not on
    if on and len(cur) > 1:
        out.append(cur)
    return out


def _prim_points(prim):
    """Uniform polyline for any line-ish primitive (mm)."""
    if prim.get("points"):
        return [(float(p[0]), float(p[1])) for p in prim["points"]]
    return []


# --------------------------------------------------------------------------- #
# PNG
# --------------------------------------------------------------------------- #
def _png_size(scene, dpi):
    canvas = scene.get("canvas") or {}
    w_mm = float(canvas.get("w_mm", 180.0))
    h_mm = float(canvas.get("h_mm", 120.0))
    w_px = int(round(w_mm / MM_PER_IN * dpi))
    h_px = int(round(h_mm / MM_PER_IN * dpi))
    return w_mm, h_mm, max(1, w_px), max(1, h_px)


def _group_text(prims):
    return [p for p in prims if p.get("type") == "text"]


def _font_size_px(size_pt, dpi, ss):
    return max(1, int(round(float(size_pt) * dpi / 72.0 * ss)))


def _pil_face(cache, fs, text, bold, dpi, ss):
    import measure
    path = fs.font_for(text, bold=bold) if fs is not None else None
    if not path:
        path = getattr(fs, "regular", None) if fs is not None else None
    if not path:
        return None
    size_px = _font_size_px(9.0, dpi, ss)
    return path, size_px


def _render_pil(scene, fs, dpi, w_px, h_px, ss=2):
    from PIL import Image, ImageDraw, ImageFont  # noqa: WPS433

    W = w_px * ss
    H = h_px * ss
    scale = dpi * ss / MM_PER_IN
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font_cache = {}

    def px(v):
        return float(v) * scale

    def font_for(line, size_pt, bold):
        import measure
        path, index = None, 0
        if fs is not None:
            path = fs.font_for(line, bold=bold)
            if path:
                index = fs.index_for(path)
        size_px = _font_size_px(size_pt, dpi, ss)
        key = (path, size_px, index)
        if key not in font_cache:
            loaded = None
            if path:
                try:
                    loaded = ImageFont.truetype(path, size_px, index=index)
                except Exception:
                    loaded = None
            if loaded is None:
                try:
                    loaded = ImageFont.load_default()
                except Exception:
                    loaded = None
            font_cache[key] = loaded
        return font_cache[key]

    for prim in scene.get("primitives") or []:
        style = prim.get("style") or {}
        ptype = prim.get("type")
        if ptype == "text":
            lines = prim.get("lines") or ([prim["text"]] if prim.get("text") else [])
            baselines = prim.get("line_baselines") or []
            size_pt = float(style.get("font_size_pt") or prim.get("font_size_pt") or 7.0)
            fill = hex_rgb255(style.get("color") or "#111111")
            anchor = style.get("anchor") or "start"
            x = float(prim.get("line_x") if prim.get("line_x") is not None
                      else (prim.get("bbox") or {}).get("x", 0))
            pil_anchor = {"start": "ls", "middle": "ms", "end": "rs"}.get(anchor, "ls")
            for i, line in enumerate(lines):
                y = float(baselines[i]) if i < len(baselines) else float(
                    (prim.get("bbox") or {}).get("y", 0))
                fnt = font_for(line, size_pt, bool(style.get("bold")))
                if fnt is None:
                    continue
                try:
                    draw.text((px(x), px(y)), line, font=fnt, fill=fill, anchor=pil_anchor)
                except Exception:
                    draw.text((px(x), px(y)), line, font=fnt, fill=fill)
            continue

        stroke = None if _no_paint(style, "stroke") else hex_rgb255(style.get("stroke"))
        fill = None if _no_paint(style, "fill") else hex_rgb255(style.get("fill"))
        width = max(1, int(round(float(style.get("stroke_width_mm", 0.3)) * scale)))
        dash = style.get("dash") or []

        if ptype in ("rect", "rrect"):
            b = prim["bbox"]
            x0, y0 = px(b["x"]), px(b["y"])
            x1, y1 = px(b["x"] + b["w"]), px(b["y"] + b["h"])
            rx = float(style.get("rx_mm") or 0.0) * scale
            if rx > 0.5:
                draw.rounded_rectangle([x0, y0, x1, y1], radius=rx, fill=fill,
                                       outline=stroke, width=width)
            else:
                draw.rectangle([x0, y0, x1, y1], fill=fill, outline=stroke, width=width)
        elif ptype in ("circle", "ellipse"):
            cx, cy = px(prim.get("cx", 0)), px(prim.get("cy", 0))
            rx = px(prim.get("r") or prim.get("rx") or 0)
            ry = px(prim.get("r") or prim.get("ry") or 0)
            draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=fill,
                         outline=stroke, width=width)
        elif ptype == "polygon":
            pts = [(px(p[0]), px(p[1])) for p in (prim.get("points") or [])]
            if len(pts) >= 3:
                draw.polygon(pts, fill=fill, outline=stroke)
                if stroke and width > 1:
                    draw.line(pts + [pts[0]], fill=stroke, width=width, joint="curve")
        elif ptype == "line":
            pts = [(px(p[0]), px(p[1])) for p in _prim_points(prim)]
            for seg in _dash_segments(pts, dash):
                if len(seg) >= 2:
                    draw.line(seg, fill=stroke or (0, 0, 0), width=width, joint="curve")
        elif ptype == "path":
            pts = _prim_points(prim)
            cubic = prim.get("cubic")
            if cubic and len(pts) >= 2:
                pts = [pts[0]] + _cubic_points(pts[0], cubic[0], cubic[1], pts[-1]) + [pts[-1]]
            ppt = [(px(p[0]), px(p[1])) for p in pts]
            for seg in _dash_segments(ppt, dash):
                if len(seg) >= 2:
                    draw.line(seg, fill=stroke or (0, 0, 0), width=width, joint="curve")
    return img


def write_png(scene, path, fs, dpi):
    """Rasterise the scene to PNG at `dpi`. Returns a metrics dict."""
    dpi = int(dpi)
    w_mm, h_mm, w_px, h_px = _png_size(scene, dpi)
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)

    engine = "none"
    notes = []
    try:
        from PIL import Image  # noqa: WPS433
        ss = 2
        img = _render_pil(scene, fs, dpi, w_px, h_px, ss=ss)
        img = img.resize((w_px, h_px), Image.LANCZOS)
        img.save(path, format="PNG", dpi=(dpi, dpi), optimize=False)
        engine = "pillow"
    except Exception as exc:  # pragma: no cover - defensive
        notes.append("pillow path failed: %s" % exc)
        try:
            _render_mpl(scene, fs, dpi, w_px, h_px, path)
            engine = "matplotlib-agg"
        except Exception as exc2:
            notes.append("matplotlib path failed: %s" % exc2)
            _render_stdlib(scene, fs, dpi, w_px, h_px, path)
            engine = "stdlib"
            notes.append("stdlib raster draws shapes only; text is not rasterised")

    data = open(path, "rb").read()
    return {
        "path": path,
        "px": [w_px, h_px],
        "dpi": dpi,
        "page_mm": [round(w_mm, 4), round(h_mm, 4)],
        "expected_px": [w_px, h_px],
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "engine": engine,
        "text_rasterised": engine != "stdlib",
        "notes": notes,
    }


def _render_mpl(scene, fs, dpi, w_px, h_px, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Ellipse, FancyBboxPatch, Polygon, Rectangle

    w_in, h_in = w_px / float(dpi), h_px / float(dpi)
    fig = plt.figure(figsize=(w_in, h_in), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, w_px)
    ax.set_ylim(h_px, 0)
    ax.axis("off")
    k = dpi / MM_PER_IN
    for prim in scene.get("primitives") or []:
        style = prim.get("style") or {}
        ptype = prim.get("type")
        stroke = None if _no_paint(style, "stroke") else style.get("stroke")
        fill = None if _no_paint(style, "fill") else style.get("fill")
        lw = float(style.get("stroke_width_mm", 0.3)) * k
        if ptype in ("rect", "rrect"):
            b = prim["bbox"]
            if style.get("rx_mm"):
                ax.add_patch(FancyBboxPatch((b["x"] * k, b["y"] * k), b["w"] * k, b["h"] * k,
                                            boxstyle="round,pad=0,rounding_size=%s"
                                            % (float(style["rx_mm"]) * k),
                                            linewidth=lw, edgecolor=stroke, facecolor=fill))
            else:
                ax.add_patch(Rectangle((b["x"] * k, b["y"] * k), b["w"] * k, b["h"] * k,
                                       linewidth=lw, edgecolor=stroke, facecolor=fill))
        elif ptype in ("circle", "ellipse"):
            cx, cy = prim.get("cx", 0) * k, prim.get("cy", 0) * k
            rx = (prim.get("r") or prim.get("rx") or 0) * k
            ry = (prim.get("r") or prim.get("ry") or 0) * k
            ax.add_patch(Ellipse((cx, cy), 2 * rx, 2 * ry, linewidth=lw,
                                 edgecolor=stroke, facecolor=fill))
        elif ptype == "polygon":
            ax.add_patch(Polygon([(p[0] * k, p[1] * k) for p in prim.get("points") or []],
                                 closed=True, linewidth=lw, edgecolor=stroke, facecolor=fill))
        elif ptype in ("line", "path"):
            pts = _prim_points(prim)
            cubic = prim.get("cubic")
            if ptype == "path" and cubic and len(pts) >= 2:
                pts = [pts[0]] + _cubic_points(pts[0], cubic[0], cubic[1], pts[-1]) + [pts[-1]]
            if len(pts) >= 2:
                ax.plot([p[0] * k for p in pts], [p[1] * k for p in pts],
                        color=stroke or "#000000", linewidth=lw,
                        linestyle=(0, tuple(style.get("dash") or [])) if style.get("dash") else "-")
        elif ptype == "text":
            lines = prim.get("lines") or ([prim["text"]] if prim.get("text") else [])
            bl = prim.get("line_baselines") or []
            size_pt = float(style.get("font_size_pt") or 7.0)
            x = float(prim.get("line_x") if prim.get("line_x") is not None
                      else (prim.get("bbox") or {}).get("x", 0)) * k
            anchor = {"start": "left", "middle": "center", "end": "right"}.get(
                style.get("anchor") or "start", "left")
            for i, line in enumerate(lines):
                y = (float(bl[i]) if i < len(bl) else 0.0) * k
                ax.text(x, y, line, fontsize=size_pt * dpi / 72.0, color=style.get("color"),
                        ha=anchor, va="baseline", family="sans-serif")
    fig.savefig(path, format="png", dpi=dpi, facecolor="white")
    plt.close(fig)


# ----------------------------- stdlib PNG ---------------------------------- #
def _render_stdlib(scene, fs, dpi, w_px, h_px, path, ss=2):
    W, H = w_px * ss, h_px * ss
    buf = bytearray(b"\xff" * (W * H * 3))
    scale = dpi * ss / MM_PER_IN

    def put(x, y, rgb):
        if 0 <= x < W and 0 <= y < H:
            o = (y * W + x) * 3
            buf[o] = rgb[0]
            buf[o + 1] = rgb[1]
            buf[o + 2] = rgb[2]

    def hline(x0, x1, y, rgb):
        if y < 0 or y >= H:
            return
        x0, x1 = sorted((int(round(x0)), int(round(x1))))
        x0, x1 = max(0, x0), min(W - 1, x1)
        for x in range(x0, x1 + 1):
            put(x, y, rgb)

    def fill_poly(pts, rgb):
        if len(pts) < 3:
            return
        ys = [p[1] for p in pts]
        for y in range(max(0, int(min(ys))), min(H - 1, int(max(ys))) + 1):
            xs = []
            n = len(pts)
            for i in range(n):
                x0, y0 = pts[i]
                x1, y1 = pts[(i + 1) % n]
                if (y0 <= y < y1) or (y1 <= y < y0):
                    t = (y - y0) / float(y1 - y0)
                    xs.append(x0 + (x1 - x0) * t)
            xs.sort()
            for i in range(0, len(xs) - 1, 2):
                hline(xs[i], xs[i + 1], y, rgb)

    for prim in scene.get("primitives") or []:
        style = prim.get("style") or {}
        ptype = prim.get("type")
        fill = None if _no_paint(style, "fill") else hex_rgb255(style.get("fill"))
        stroke = None if _no_paint(style, "stroke") else hex_rgb255(style.get("stroke"))
        if ptype == "text":
            continue
        if ptype in ("rect", "rrect"):
            b = prim["bbox"]
            pts = [(p[0] * scale, p[1] * scale) for p in
                   _rect_path(b["x"], b["y"], b["w"], b["h"], style.get("rx_mm"))]
            if fill:
                fill_poly(pts, fill)
        elif ptype in ("circle", "ellipse"):
            cx, cy = prim.get("cx", 0) * scale, prim.get("cy", 0) * scale
            rx = (prim.get("r") or prim.get("rx") or 0) * scale
            ry = (prim.get("r") or prim.get("ry") or 0) * scale
            if fill:
                fill_poly([(cx + rx * math.cos(2 * math.pi * i / 48),
                            cy + ry * math.sin(2 * math.pi * i / 48)) for i in range(48)], fill)
        elif ptype == "polygon":
            pts = [(p[0] * scale, p[1] * scale) for p in prim.get("points") or []]
            if fill:
                fill_poly(pts, fill)
        elif ptype in ("line", "path"):
            pts = _prim_points(prim)
            if ptype == "path" and prim.get("cubic") and len(pts) >= 2:
                pts = [pts[0]] + _cubic_points(pts[0], prim["cubic"][0], prim["cubic"][1],
                                               pts[-1]) + [pts[-1]]
            rgb = stroke or (0, 0, 0)
            w = max(1, int(round(float(style.get("stroke_width_mm", 0.3)) * scale)))
            for a, b in zip(pts, pts[1:]):
                x0, y0, x1, y1 = a[0] * scale, a[1] * scale, b[0] * scale, b[1] * scale
                n = max(1, int(max(abs(x1 - x0), abs(y1 - y0))))
                for i in range(n + 1):
                    t = i / float(n)
                    for dy in range(-(w // 2), w // 2 + 1):
                        put(int(round(x0 + (x1 - x0) * t)), int(round(y0 + (y1 - y0) * t)) + dy, rgb)
    if ss != 1:
        out = bytearray(W // ss * H // ss * 3)
        for y in range(H // ss):
            for x in range(W // ss):
                o = (y * W // ss + x) * 3
                acc = [0, 0, 0]
                for dy in range(ss):
                    for dx in range(ss):
                        s = ((y * ss + dy) * W + (x * ss + dx)) * 3
                        acc[0] += buf[s]
                        acc[1] += buf[s + 1]
                        acc[2] += buf[s + 2]
                n = ss * ss
                out[o] = acc[0] // n
                out[o + 1] = acc[1] // n
                out[o + 2] = acc[2] // n
        buf = out
        W, H = W // ss, H // ss

    raw = bytearray()
    stride = W * 3
    for y in range(H):
        raw.append(0)
        raw += buf[y * stride:(y + 1) * stride]
    png = _png_chunks(W, H, bytes(raw), dpi)
    with open(path, "wb") as fh:
        fh.write(png)


def _png_chunks(w, h, raw, dpi):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ppm = int(round(dpi / 0.0254))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    phys = struct.pack(">IIB", ppm, ppm, 1)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"pHYs", phys) + chunk(b"IDAT", zlib.compress(raw, 9)) +
            chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# PDF (single page, vector, embedded TrueType)
# --------------------------------------------------------------------------- #
_PDF_FONT_CACHE = {}


def _pdf_name(value, fallback="Font"):
    out = "".join(ch if (ch.isalnum() or ch in "-_.") else "" for ch in str(value or ""))
    return out or fallback


def _subset_font(path, index, codes):
    """Return (font_program_bytes, used_cmap). Subsets when possible."""
    from fontTools.ttLib import TTFont  # noqa: WPS433
    font = TTFont(path, fontNumber=index, lazy=True)
    full = open(path, "rb").read()
    program = full
    try:
        from fontTools import subset  # noqa: WPS433
        options = subset.Options()
        options.retain_gids = True
        options.glyph_names = False
        options.notdef_outline = True
        options.hinting = False
        options.desubroutinize = False
        options.recalc_bounds = False
        options.recalc_timestamp = False      # keeps the subset byte-stable
        options.drop_tables += ["DSIG", "EBDT", "EBLC", "gasp", "meta"]
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=sorted(codes))
        subsetter.subset(font)
        try:                                   # belt and braces: freeze head dates
            head = font["head"]
            head.created = 0
            head.modified = 0
        except Exception:
            pass
        import io
        buf = io.BytesIO()
        # TTFont.save() has its OWN recalcTimestamp flag (default True) which
        # rewrites head.modified to "now" -- the subset Option of the same name
        # does not cover it. Without this the embedded /FontFile2 stream (and so
        # the whole PDF) changes on every run even though the drawing does not.
        font.save(buf, reorderTables=True, recalcTimestamp=False)
        candidate = buf.getvalue()
        if _has_table(candidate, b"glyf"):
            program = candidate
    except Exception:
        program = full
    return program, font


def _has_table(sfnt, tag):
    try:
        n = struct.unpack(">H", sfnt[4:6])[0]
        for i in range(n):
            off = 12 + 16 * i
            if sfnt[off:off + 4] == tag:
                return True
    except Exception:
        pass
    return False


def _pdf_escape_name(name):
    return "".join(ch if ch.isalnum() else "#%02X" % ord(ch) for ch in name)


def write_pdf(scene, path, fs):
    """Single-page vector PDF with embedded TrueType (contract §4/§6 H)."""
    canvas = scene.get("canvas") or {}
    W_mm = float(canvas.get("w_mm", 180.0))
    H_mm = float(canvas.get("h_mm", 120.0))
    W_pt = W_mm * MM_PT
    H_pt = H_mm * MM_PT
    prims = list(scene.get("primitives") or [])

    # ---- 1. collect the faces and the characters they must cover ----------- #
    faces = {}          # path -> {"index","codes":set,"chars":{}}
    order = []
    for prim in prims:
        if prim.get("type") != "text":
            continue
        style = prim.get("style") or {}
        bold = bool(style.get("bold"))
        lines = prim.get("lines") or ([prim["text"]] if prim.get("text") else [])
        for line in lines:
            p = fs.font_for(line, bold=bold) if fs is not None else None
            p = p or getattr(fs, "regular", None)
            if not p:
                continue
            if p not in faces:
                faces[p] = {"index": fs.index_for(p) if fs is not None else 0,
                            "codes": set(), "chars": {}}
                order.append(p)
            for ch in line:
                if ch in "\n\r\t":
                    continue
                faces[p]["codes"].add(ord(ch))
                faces[p]["chars"][ord(ch)] = ch

    font_info = {}
    for p in order:
        try:
            program, font = _subset_font(p, faces[p]["index"], faces[p]["codes"])
        except Exception:
            continue
        upem = float(font["head"].unitsPerEm) or 1000.0
        cmap = font.getBestCmap() or {}
        hmtx = font["hmtx"].metrics
        head = font["head"]
        hhea = font["hhea"]
        try:
            os2 = font["OS/2"]
            cap = float(getattr(os2, "sCapHeight", 0) or 0) or float(hhea.ascent) * 0.7
        except Exception:
            cap = float(hhea.ascent) * 0.7
        ps_name = None
        try:
            for rec in font["name"].names:
                if rec.nameID == 6:
                    ps_name = rec.toUnicode()
                    break
        except Exception:
            ps_name = None
        gids = {}
        widths = {}
        for code in sorted(faces[p]["codes"]):
            gname = cmap.get(code)
            if gname is None:
                continue
            gid = font.getGlyphID(gname)
            gids[code] = gid
            adv = hmtx.get(gname)
            widths[gid] = int(round((adv[0] if adv else upem) * 1000.0 / upem))
        font_info[p] = {
            "program": program,
            "upem": upem,
            "gids": gids,
            "widths": widths,
            "base": _pdf_name(ps_name or os.path.basename(p).split(".")[0], "Embedded"),
            "bbox": [float(head.xMin) * 1000.0 / upem, float(head.yMin) * 1000.0 / upem,
                     float(head.xMax) * 1000.0 / upem, float(head.yMax) * 1000.0 / upem],
            "ascent": float(hhea.ascent) * 1000.0 / upem,
            "descent": float(hhea.descent) * 1000.0 / upem,
            "capheight": cap * 1000.0 / upem,
            "flags": 4,
            "stemv": 80,
            "italic": 0.0,
        }
        try:
            font.close()
        except Exception:
            pass

    # resource names must exist before the content stream is serialised
    for i, p in enumerate([q for q in order if q in font_info]):
        font_info[p]["res"] = "F%d" % (i + 1)

    # ---- 2. content stream ------------------------------------------------- #
    K = MM_PT

    def X(v):
        return v * K

    def Y(v):
        return H_pt - v * K

    def col(style, key, op):
        rgb = hex_rgb(style.get(key))
        return "%.4f %.4f %.4f %s" % (rgb[0], rgb[1], rgb[2], op)

    def path_ops(pts, close=False):
        out = []
        if not pts:
            return out
        out.append("%.4f %.4f m" % (X(pts[0][0]), Y(pts[0][1])))
        for p in pts[1:]:
            out.append("%.4f %.4f l" % (X(p[0]), Y(p[1])))
        if close:
            out.append("h")
        return out

    def bezier_ellipse(cx, cy, rx, ry):
        k = 0.5522847498
        pts = [((cx + rx), cy), (cx, (cy + ry)), ((cx - rx), cy), (cx, (cy - ry))]
        out = ["%.4f %.4f m" % (X(pts[0][0]), Y(pts[0][1]))]
        ctrl = [(k * rx, 0, 0, k * ry), (0, k * ry, -k * rx, 0),
                (-k * rx, 0, 0, -k * ry), (0, -k * ry, k * rx, 0)]
        order_pts = [pts[1], pts[2], pts[3], pts[0]]
        cur = pts[0]
        for (target, (c1x, c1y, c2x, c2y)) in zip(order_pts, ctrl):
            out.append("%.4f %.4f %.4f %.4f %.4f %.4f c"
                       % (X(cur[0] + c1x), Y(cur[1] + c1y),
                          X(target[0] + c2x), Y(target[1] + c2y),
                          X(target[0]), Y(target[1])))
            cur = target
        out.append("h")
        return out

    def rrect_ops(b, rx):
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        rx = min(rx, w / 2.0, h / 2.0)
        k = 0.5522847498
        out = ["%.4f %.4f m" % (X(x + rx), Y(y))]
        out.append("%.4f %.4f l" % (X(x + w - rx), Y(y)))
        out.append("%.4f %.4f %.4f %.4f %.4f %.4f c"
                   % (X(x + w - rx + k * rx), Y(y), X(x + w), Y(y + rx - k * rx),
                      X(x + w), Y(y + rx)))
        out.append("%.4f %.4f l" % (X(x + w), Y(y + h - rx)))
        out.append("%.4f %.4f %.4f %.4f %.4f %.4f c"
                   % (X(x + w), Y(y + h - rx + k * rx), X(x + w - rx + k * rx), Y(y + h),
                      X(x + w - rx), Y(y + h)))
        out.append("%.4f %.4f l" % (X(x + rx), Y(y + h)))
        out.append("%.4f %.4f %.4f %.4f %.4f %.4f c"
                   % (X(x + rx - k * rx), Y(y + h), X(x), Y(y + h - rx + k * rx),
                      X(x), Y(y + h - rx)))
        out.append("%.4f %.4f l" % (X(x), Y(y + rx)))
        out.append("%.4f %.4f %.4f %.4f %.4f %.4f c"
                   % (X(x), Y(y + rx - k * rx), X(x + rx - k * rx), Y(y),
                      X(x + rx), Y(y)))
        out.append("h")
        return out

    content = []
    for prim in prims:
        style = prim.get("style") or {}
        ptype = prim.get("type")
        if ptype == "text":
            lines = prim.get("lines") or ([prim["text"]] if prim.get("text") else [])
            bl = prim.get("line_baselines") or []
            size_pt = float(style.get("font_size_pt") or 7.0)
            anchor = style.get("anchor") or "start"
            x = float(prim.get("line_x") if prim.get("line_x") is not None
                      else (prim.get("bbox") or {}).get("x", 0))
            for i, line in enumerate(lines):
                p = fs.font_for(line, bold=bool(style.get("bold"))) if fs is not None else None
                info = font_info.get(p)
                if info is None:
                    continue
                y = float(bl[i]) if i < len(bl) else 0.0
                hexes = []
                width_pt = 0.0
                for ch in line:
                    gid = info["gids"].get(ord(ch), 0)
                    hexes.append("%04X" % (gid & 0xFFFF))
                    width_pt += info["widths"].get(gid, 1000) * size_pt / 1000.0
                if anchor == "middle":
                    x_draw = x - width_pt / (2.0 * K)
                elif anchor == "end":
                    x_draw = x - width_pt / K
                else:
                    x_draw = x
                content.append("q")
                content.append(col(style, "color", "rg"))
                content.append("BT /%s %.4f Tf 1 0 0 1 %.4f %.4f Tm <%s> Tj ET"
                               % (info["res"], size_pt, X(x_draw), Y(y), "".join(hexes)))
                content.append("Q")
            continue

        has_fill = not _no_paint(style, "fill")
        has_stroke = not _no_paint(style, "stroke")
        if not has_fill and not has_stroke:
            continue
        content.append("q")
        if has_stroke:
            content.append("%.4f w" % (float(style.get("stroke_width_mm", 0.3)) * K))
            content.append("1 J 1 j")
            content.append(col(style, "stroke", "RG"))
            dash = style.get("dash") or []
            if dash:
                content.append("[%s] 0 d" % " ".join("%.4f" % (float(v) * K) for v in dash))
        if has_fill:
            content.append(col(style, "fill", "rg"))
        paint = "B" if (has_fill and has_stroke) else ("f" if has_fill else "S")
        if ptype in ("rect", "rrect"):
            b = prim["bbox"]
            if has_fill and has_stroke and (style.get("rx_mm") or 0):
                paint = "B"
            if style.get("rx_mm"):
                content.extend(rrect_ops(b, float(style["rx_mm"])))
            else:
                content.append("%.4f %.4f %.4f %.4f re"
                               % (X(b["x"]), Y(b["y"] + b["h"]), X(b["w"]), X(b["h"])))
            content.append(paint)
        elif ptype in ("circle", "ellipse"):
            r = prim.get("r") or prim.get("rx") or 0
            rx = prim.get("r") or prim.get("rx") or r
            ry = prim.get("r") or prim.get("ry") or r
            content.extend(bezier_ellipse(prim.get("cx", 0), prim.get("cy", 0), rx, ry))
            content.append(paint)
        elif ptype == "polygon":
            content.extend(path_ops(_prim_points(prim), close=True))
            content.append(paint)
        elif ptype in ("line", "path"):
            pts = _prim_points(prim)
            cubic = prim.get("cubic")
            dash = style.get("dash") or []
            if ptype == "path" and cubic and len(pts) >= 2:
                content.append("%.4f %.4f m" % (X(pts[0][0]), Y(pts[0][1])))
                content.append("%.4f %.4f %.4f %.4f %.4f %.4f c"
                               % (X(cubic[0][0]), Y(cubic[0][1]),
                                  X(cubic[1][0]), Y(cubic[1][1]),
                                  X(pts[-1][0]), Y(pts[-1][1])))
                content.append("S")
            else:
                for seg in _dash_segments(pts, dash):
                    if len(seg) < 2:
                        continue
                    content.extend(path_ops(seg))
                    content.append("S")
        content.append("Q")

    stream_text = "\n".join(content) + "\n"
    stream = zlib.compress(stream_text.encode("latin-1"), 9)

    # ---- 3. objects -------------------------------------------------------- #
    objs = {}

    def add(body):
        n = len(objs) + 1
        objs[n] = body
        return n

    cat = add(b"")           # placeholder, filled after page count is known
    pages = add(b"")
    page = add(b"")
    content_id = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
                     + stream + b"\nendstream")

    resources = []
    for p in order:
        info = font_info.get(p)
        if info is None:
            continue
        program = info["program"]
        packed = zlib.compress(program, 9)
        ff = add(b"<< /Length %d /Length1 %d /Filter /FlateDecode >>\nstream\n"
                 % (len(packed), len(program)) + packed + b"\nendstream")
        fd = add(("<< /Type /FontDescriptor /FontName /%s /Flags %d /FontBBox [%s] "
                  "/ItalicAngle %.2f /Ascent %.1f /Descent %.1f /CapHeight %.1f "
                  "/StemV %d /FontFile2 %d 0 R >>"
                  % (_pdf_escape_name(info["base"]), info["flags"],
                     " ".join("%.1f" % v for v in info["bbox"]), info["italic"],
                     info["ascent"], info["descent"], info["capheight"],
                     info["stemv"], ff)).encode("latin-1"))
        w_items = []
        for gid in sorted(info["widths"]):
            w_items.append("%d [%d]" % (gid, info["widths"][gid]))
        cid = add(("<< /Type /Font /Subtype /CIDFontType2 /BaseFont /%s "
                   "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
                   "/Supplement 0 >> /FontDescriptor %d 0 R /DW 1000 /W [%s] "
                   "/CIDToGIDMap /Identity >>"
                   % (_pdf_escape_name(info["base"]), fd, " ".join(w_items))).encode("latin-1"))
        tu = add(_tounicode(faces[p]["chars"], info["gids"]))
        t0 = add(("<< /Type /Font /Subtype /Type0 /BaseFont /%s /Encoding /Identity-H "
                  "/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>"
                  % (_pdf_escape_name(info["base"]), cid, tu)).encode("latin-1"))
        info["obj"] = t0
        resources.append(t0)

    font_dict = " ".join("/F%d %d 0 R" % (i + 1, obj) for i, obj in enumerate(resources))
    objs[page] = ("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.4f %.4f] "
                  "/Resources << /Font << %s >> >> /Contents %d 0 R >>"
                  % (pages, W_pt, H_pt, font_dict, content_id)).encode("latin-1")
    objs[pages] = ("<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page).encode("latin-1")
    objs[cat] = ("<< /Type /Catalog /Pages %d 0 R >>" % pages).encode("latin-1")
    info_id = add(("<< /Producer (graphical-abstract/raster_backend.py) "
                   "/Creator (graphical-abstract skill) /Title (%s) >>"
                   % _pdf_text((scene.get("meta") or {}).get("title") or "Graphical abstract")
                   ).encode("latin-1"))

    # re-map resource names now that every font object exists
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    xref_pos = len(out)
    count = max(objs) + 1
    out += b"xref\n0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for n in range(1, count):
        out += b"%010d 00000 n \n" % offsets.get(n, 0)
    out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (count, cat, info_id, xref_pos))

    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "wb") as fh:
        fh.write(bytes(out))

    data = open(path, "rb").read()
    embedded = [os.path.basename(p) for p in order if p in font_info]
    return {
        "path": path,
        "pages": 1,
        "page_mm": [round(W_mm, 4), round(H_mm, 4)],
        "page_pt": [round(W_pt, 4), round(H_pt, 4)],
        "mediabox_pt": [round(W_pt, 4), round(H_pt, 4)],
        "fonts_embedded": bool(embedded),
        "fonts": embedded,
        "fontfile2_count": data.count(b"/FontFile2"),
        "vector": True,
        "text_operators": stream_text.count(" Tj") + stream_text.count("TJ"),
        "text_show_calls": stream_text.count("Tj"),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "engine": "stdlib-pdf",
        "notes": [] if embedded else ["no TrueType face available to embed"],
    }


def _pdf_text(value):
    return "".join(ch if 32 <= ord(ch) < 127 and ch not in "()\\" else "?" for ch in str(value))


def _tounicode(chars, gids):
    pairs = []
    for code in sorted(chars):
        gid = gids.get(code)
        if gid is None:
            continue
        pairs.append("<%04X> <%04X>" % (gid & 0xFFFF, code))
    body = ["/CIDInit /ProcSet findresource begin",
            "12 dict begin", "begincmap",
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
            "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
            "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
    for i in range(0, len(pairs), 100):
        block = pairs[i:i + 100]
        body.append("%d beginbfchar" % len(block))
        body.extend(block)
        body.append("endbfchar")
    body += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    text = "\n".join(body).encode("latin-1")
    return (b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream")

#!/usr/bin/env python3
"""layout.py — deterministic spec -> scene layout engine (contract §4, §6).

    layout(spec, fs) -> {"canvas", "primitives", "panels", "warnings", "meta"}

Everything is in MILLIMETRES, origin top-left, +y down (contract §2).  Nothing
in this module reads the clock, calls random(), or iterates a set for output
order, so the same spec always produces the same scene and the same bytes
downstream.

Layout strategy
---------------
1. Bands are measured top-down: figure title, then the panel grid, then the
   caption/conclusion strip, all measured with real glyph advances.
2. Each panel is *measured* before it is drawn: for every candidate grid
   (node columns x chart placement) the required height is computed from the
   wrapped label/sublabel lines.  The cheapest feasible arrangement wins.
3. Row heights come from those measurements, so a panel never has to scale its
   rows down — the failure mode that used to drop whole labels.  If the total
   still does not fit, the whole type scale steps down (to the contract floors
   of 11 / 8 / 7 pt) and the panels are re-measured; only when the floors are
   reached is a warning emitted, and it reports the real numbers.

Required text is never dropped: `W_TEXT_FLOOR` means "did not fit at the floor"
and `W_TEXT_TRUNCATED` is reserved for the typographic shortfall it names.

See ``SCENE.md`` for the exact field list every primitive carries.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import math

import measure
import scene

VERSION = "1.2"
GENERATOR = "data-chart/layout.py %s" % VERSION

# --------------------------------------------------------------------------- #
# geometry constants (mm)
# --------------------------------------------------------------------------- #
GUTTER_MM = 3.0
PANEL_PAD_MM = 1.8
PANEL_HEADER_MIN_MM = 5.6
NODE_PAD_Y = 1.2
NODE_GAP_X = 1.6
NODE_GAP_Y = 1.4
MIN_NODE_W_MM = 20.0
MIN_NODE_H_MM = 6.0
EDGE_W_MM = 0.45
EDGE_HEAD_MM = 1.6
LEGEND_ROW_MM = 3.4
LEGEND_W_MM = 30.0
COLORBAR_W_MM = 15.0
COLORBAR_V_MIN_W = 34.0
TICK_LEN_MM = 1.0
MIN_DIAGRAM_MM = 12.0
CHART_MIN_W_MM = 44.0
CHART_MIN_H_MM = 30.0
CHART_PREF_H_MM = 40.0
BAND_GAP_MM = 1.6
COLORBAR_V_MIN_H = 26.0
COLORBAR_H_H = 11.0
LEGEND_COL_W_MM = 24.0

NEUTRAL_FILL = "#E8E8E8"
INK = "#111111"
MUTED = "#444444"
FRAME = "#333333"
GRID = "#DDDDDD"
PAGE_BG = "#FFFFFF"

# Base type sizes (pt); every one has a contract floor below.
BASE_SIZES = {
    "title": 13.0,
    "subtitle": 8.5,
    "caption": 7.5,
    "panel_title": 9.5,
    "panel_role": 7.0,
    "node_label": 8.0,
    "node_sub": 7.0,
    "edge_label": 7.0,
    "axis": 7.0,
    "chart_title": 7.5,
    "legend": 7.0,
    "colorbar": 7.0,
}
FLOOR_SIZES = {
    "title": scene.MIN_MAIN_TITLE_PT,
    "subtitle": scene.MIN_BODY_PT,
    "caption": scene.MIN_BODY_PT,
    "panel_title": scene.MIN_PANEL_TITLE_PT,
    "panel_role": scene.MIN_BODY_PT,
    "node_label": scene.MIN_BODY_PT,
    "node_sub": scene.MIN_BODY_PT,
    "edge_label": scene.MIN_BODY_PT,
    "axis": scene.MIN_BODY_PT,
    "chart_title": scene.MIN_BODY_PT,
    "legend": scene.MIN_BODY_PT,
    "colorbar": scene.MIN_BODY_PT,
}

DASHES = {
    "activation": [],
    "inhibition": [],
    "flow": [2.0, 1.2],
    "spatial": [0.6, 1.0],
    "feedback": [],
    "translocation": [3.0, 1.0, 0.8, 1.0],
}

LAYER_ORDER = ["frame", "panel", "chart", "entity", "relation",
               "legend", "colorbar", "annotation"]


SPACING_LEVELS = [
    {"gap": 1.6, "node_gap_y": 1.4, "node_pad_y": 1.2, "leading": 1.22,
     "label_leading": 1.18, "legend_row": 3.4, "panel_pad": 1.8, "hdr_pad": 1.0},
    {"gap": 1.2, "node_gap_y": 1.0, "node_pad_y": 0.9, "leading": 1.16,
     "label_leading": 1.12, "legend_row": 3.0, "panel_pad": 1.4, "hdr_pad": 0.8},
    {"gap": 0.9, "node_gap_y": 0.7, "node_pad_y": 0.6, "leading": 1.10,
     "label_leading": 1.06, "legend_row": 2.8, "panel_pad": 1.1, "hdr_pad": 0.6},
]


def base_sizes(spec):
    """BASE_SIZES with the spec's ``meta.typography`` overrides (CONTRACT §8).

    A preset may only move a size *up to* its contract floor: anything smaller is
    clamped here as well as in ``presets.resolve_typography``, so a journal that
    asks for 5 pt body text cannot quietly drop below gate B.
    """
    out = dict(BASE_SIZES)
    meta = (spec or {}).get("meta") or {}
    typo = meta.get("typography") if isinstance(meta.get("typography"), dict) else {}
    for key, val in typo.items():
        if key in out and isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = max(FLOOR_SIZES[key], float(val))
    return out


def stroke_widths(spec):
    """Data-stroke widths (mm) from ``meta.line_widths_pt`` (CONTRACT §8)."""
    meta = (spec or {}).get("meta") or {}
    lw = meta.get("line_widths_pt") if isinstance(meta.get("line_widths_pt"), dict) else {}
    out = {"axis": 0.30, "data": 0.70, "error": 0.35, "frame": 0.30}
    mapping = {"axis": "axis", "data": "data", "error": "error", "frame": "frame"}
    for key, mm_key in mapping.items():
        pt = lw.get(key)
        if isinstance(pt, (int, float)) and not isinstance(pt, bool) and pt > 0:
            out[mm_key] = round(float(pt) * scene.PT_MM, 4)
    return out


def scaled_sizes(scale, level=0, base=None):
    """Type sizes (pt) for a scale factor, plus the spacing step for that level."""
    src = base or BASE_SIZES
    out = {}
    for key, b in src.items():
        out[key] = max(FLOOR_SIZES[key], round(float(b) * scale, 2))
    out["__spacing__"] = dict(SPACING_LEVELS[max(0, min(level, len(SPACING_LEVELS) - 1))])
    out["__scale__"] = round(float(scale), 4)
    return out


def sp_of(sizes):
    return sizes.get("__spacing__") or SPACING_LEVELS[0]


# --------------------------------------------------------------------------- #
# small deterministic helpers
# --------------------------------------------------------------------------- #
def _r(v, nd=4):
    out = round(float(v), nd)
    return 0.0 if out == 0.0 else out


def _bbox(x, y, w, h, stroke=0.0):
    pad = abs(float(stroke)) / 2.0 if stroke else 0.0
    return {"x": _r(float(x) - pad), "y": _r(float(y) - pad),
            "w": _r(max(0.0, float(w)) + 2 * pad),
            "h": _r(max(0.0, float(h)) + 2 * pad)}


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _hex_rgb(color):
    c = (color or "#000000").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) == 6:
        try:
            return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        except ValueError:
            pass
    return 0, 0, 0


def _rgb_hex(rgb):
    return "#%02X%02X%02X" % tuple(int(_clamp(round(v), 0, 255)) for v in rgb)


def cmap_color(cmap_id, t):
    """Sequential colour for t in [0,1] from scene.CMAPS (linear RGB stops)."""
    stops = scene.CMAPS.get(cmap_id) or scene.CMAPS["grey"]
    t = _clamp(float(t), 0.0, 1.0)
    n = len(stops) - 1
    if n <= 0:
        return stops[0]
    pos = t * n
    i = int(math.floor(pos))
    if i >= n:
        i, frac = n - 1, 1.0
    else:
        frac = pos - i
    a, b = _hex_rgb(stops[i]), _hex_rgb(stops[i + 1])
    return _rgb_hex([a[k] + (b[k] - a[k]) * frac for k in range(3)])


def _channel_lum(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color):
    r, g, b = _hex_rgb(color)
    return 0.2126 * _channel_lum(r) + 0.7152 * _channel_lum(g) + 0.0722 * _channel_lum(b)


def contrast_ratio(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ink_on(fill, prefer_dark=INK, prefer_light="#FFFFFF", minimum=4.5):
    """Text colour with the best measured WCAG contrast against `fill`."""
    dark = contrast_ratio(prefer_dark, fill)
    light = contrast_ratio(prefer_light, fill)
    best = prefer_dark if dark >= light else prefer_light
    if max(dark, light) >= minimum:
        return best
    return prefer_dark if dark >= light else prefer_light


readable_ink = ink_on


def fmt_num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f) and abs(f) < 1e6:
        return "%d" % int(f)
    a = abs(f)
    if a >= 100:
        s = "%.0f" % f
    elif a >= 10:
        s = "%.1f" % f
    elif a >= 1:
        s = "%.2f" % f
    else:
        s = "%.3f" % f
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# --------------------------------------------------------------------------- #
# builder
# --------------------------------------------------------------------------- #
class _Builder(object):
    def __init__(self, spec, fs, sizes):
        self.spec = spec
        self.fs = fs
        self.sizes = sizes
        self.seq = 0
        self.primitives = []
        self.warnings = []
        self.marks = []            # CONTRACT §4.1 mark records (datamap input)
        self._mark_ids = set()
        self.widths = stroke_widths(spec)
        self.palette = scene.resolve_palette(spec)
        self.evidence = {}
        for ev in (spec.get("evidence") or []):
            if isinstance(ev, dict) and isinstance(ev.get("id"), str):
                self.evidence[ev["id"]] = ev
        self._uid_seen = set()

    # -- ids & bookkeeping -------------------------------------------------- #
    def uid(self, prefix, key):
        self.seq += 1
        seed = "%d|%s" % (self.seq, key)
        out = scene.stable_id(prefix, seed)
        guard = 0
        while out in self._uid_seen:
            guard += 1
            out = scene.stable_id(prefix, seed + "|%d" % guard)
        self._uid_seen.add(out)
        return out

    def warn(self, code, message, panel=None):
        self.warnings.append({"code": code, "severity": "warn",
                              "message": message, "panel": panel})

    def mark(self, mark_id, **fields):
        """Record one drawn data mark (CONTRACT §4.1).

        ``mark_id``s are unique by construction; a collision is disambiguated
        deterministically rather than silently merged, because datamap keys cells
        on them.
        """
        out = str(mark_id)
        guard = 0
        while out in self._mark_ids:
            guard += 1
            out = "%s#%d" % (mark_id, guard)
        self._mark_ids.add(out)
        rec = {"mark_id": out}
        rec.update(fields)
        self.marks.append(rec)
        return rec

    def src_of(self, ev_id):
        ev = self.evidence.get(ev_id)
        if not isinstance(ev, dict):
            return None
        src = ev.get("source")
        if not isinstance(src, dict):
            return None
        return {"file": src.get("file"), "locator": src.get("locator"),
                "quote": src.get("quote")}

    def evidence_for(self, obj):
        """Accept `source` (id) or `sources` (list of ids) from a spec node."""
        ids = []
        if isinstance(obj, dict):
            if isinstance(obj.get("source"), str) and obj["source"]:
                ids.append(obj["source"])
            for key in ("sources", "evidence"):
                val = obj.get(key)
                if isinstance(val, list):
                    ids.extend([v for v in val if isinstance(v, str) and v])
                elif isinstance(val, str) and val:
                    ids.append(val)
        seen, out = set(), []
        for i in ids:
            if i not in seen and i in self.evidence:
                seen.add(i)
                out.append(i)
        return out

    # -- primitive factory -------------------------------------------------- #
    def add(self, layer, ptype, bbox, element, panel=None, evidence=None,
            style=None, **extra):
        prefix = {"frame": "bg", "panel": "pn", "chart": "ch", "entity": "en",
                  "relation": "rl", "legend": "lg", "colorbar": "cb",
                  "annotation": "an"}.get(layer, "el")
        ev = None
        if isinstance(evidence, str):
            ev = evidence
        elif isinstance(evidence, (list, tuple)) and evidence:
            ev = evidence[0]
        prim = {
            "id": self.uid(prefix, "%s|%s|%s|%s" % (layer, element, panel or "", bbox)),
            "layer": layer,
            "type": ptype,
            "bbox": bbox,
            "element": element,
            "style": dict(style or {}),
            "panel": panel,
            "evidence": ev,
            "evidence_ids": list(evidence) if isinstance(evidence, (list, tuple))
            else ([ev] if ev else []),
            "source": self.src_of(ev) if ev else None,
        }
        prim.update(extra)
        self.primitives.append(prim)
        return prim

    # -- text --------------------------------------------------------------- #
    def text(self, layer, element, value, x, y, w, h, size_key, *,
             bold=False, color=INK, align="left", valign="top", leading=1.25,
             panel=None, evidence=None, background=None, max_lines=None,
             rotate=None, allow_truncate=True):
        """Emit a text primitive whose box already fits the wrapped lines.

        The box height is *recomputed* from the wrapped lines (`n_lines *
        line_height`) and may grow downwards inside the caller's allowance;
        lines are only ever dropped when the caller explicitly allows it, and
        that always raises a warning.
        """
        if isinstance(size_key, str):
            size_pt = self.sizes[size_key]
            floor = FLOOR_SIZES[size_key]
        else:
            size_pt, floor = float(size_key), scene.MIN_BODY_PT
        value = "" if value is None else str(value)
        fit = measure.fit_text(value, size_pt, self.fs, w, h, min_pt=floor,
                               leading=leading, bold=bold)
        lines = list(fit["lines"])
        lh = measure.line_height_mm(fit["size_pt"], leading)
        fits = max(1, int(math.floor(h / lh + 1e-9)))
        if max_lines is not None:
            fits = min(fits, max_lines)
        dropped = 0
        if len(lines) > fits:
            if allow_truncate:
                dropped = len(lines) - fits
                lines = lines[:fits]
            else:
                # never lose a required label: keep every line and record the
                # overflow instead of silently cropping the text
                self.warn("W_TEXT_OVERFLOW",
                          "%d line(s) of %r do not fit a %.1f mm box at %.1f pt "
                          "(needs %.1f mm)" % (len(lines) - fits, value[:60], h,
                                               fit["size_pt"],
                                               len(lines) * lh), panel=panel)
        block_h = lh * len(lines)
        if valign == "middle":
            top = y + (h - block_h) / 2.0
        elif valign == "bottom":
            top = y + h - block_h
        else:
            top = y
        if top < y - 1e-9:
            top = y
        ascent = 0.80 * fit["size_pt"] * scene.PT_MM
        anchor = {"center": "middle", "right": "end"}.get(align, "start")
        tx = x + w / 2.0 if align == "center" else (x + w if align == "right" else x)
        baselines = [_r(top + i * lh + ascent) for i in range(len(lines))]
        widest = max([measure.text_width_mm(ln, fit["size_pt"], self.fs, bold)
                      for ln in lines] or [0.0])
        if fit["floor_hit"] and value.strip():
            self.warn("W_TEXT_FLOOR",
                      "text hit the %.1f pt floor in a %.1f x %.1f mm box "
                      "(needs %.1f mm wide): %r"
                      % (floor, w, h, fit["width_mm"], value[:60]), panel=panel)
        if dropped:
            self.warn("W_TEXT_TRUNCATED",
                      "%d wrapped line(s) of %r dropped: box is %.1f mm tall, "
                      "%d line(s) fit at %.1f pt"
                      % (dropped, value[:60], h, fits, fit["size_pt"]), panel=panel)
        style = {
            "font_size_pt": _r(fit["size_pt"], 3),
            "bold": bool(bold),
            "color": color,
            "align": align,
            "valign": valign,
            "anchor": anchor,
            "leading": _r(leading, 3),
        }
        if background:
            style["background"] = background
            style["contrast"] = _r(contrast_ratio(color, background), 3)
        if rotate:
            style["rotate_deg"] = _r(rotate, 3)
        return self.add(layer, "text", _bbox(x, y, w, max(block_h, 0.4)), element,
                        panel=panel, evidence=evidence, style=style,
                        text=value, lines=lines, line_x=_r(tx, 4),
                        line_baselines=baselines,
                        font_size_pt=_r(fit["size_pt"], 3),
                        max_w_mm=_r(w, 4), measured_w_mm=_r(widest, 4),
                        measured_h_mm=_r(block_h, 4), line_height_mm=_r(lh, 4),
                        floor_hit=bool(fit["floor_hit"]),
                        shrunk=bool(fit["shrunk"]), dropped_lines=dropped)

    # -- decor -------------------------------------------------------------- #
    def rect(self, layer, element, bbox, fill="none", stroke="none",
             width=0.3, rx=None, panel=None, evidence=None, dash=None, **extra):
        style = {"fill": fill, "stroke": stroke, "stroke_width_mm": _r(width, 3)}
        if rx is not None:
            style["rx_mm"] = _r(rx, 3)
        if dash:
            style["dash"] = list(dash)
        ptype = "rrect" if rx else "rect"
        return self.add(layer, ptype, bbox, element, panel=panel,
                        evidence=evidence, style=style, **extra)

    def line(self, layer, element, p1, p2, stroke=FRAME, width=0.3,
             panel=None, evidence=None, dash=None, ptype="line", **extra):
        style = {"stroke": stroke, "stroke_width_mm": _r(width, 3),
                 "linecap": "butt"}
        if dash:
            style["dash"] = list(dash)
        pts = [[_r(p1[0]), _r(p1[1])], [_r(p2[0]), _r(p2[1])]]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return self.add(layer, ptype,
                        _bbox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys),
                              stroke=width),
                        element, panel=panel, evidence=evidence, style=style,
                        points=pts, **extra)

    def polygon(self, layer, element, points, fill="none", stroke="none",
                width=0.3, panel=None, evidence=None, **extra):
        pts = [[_r(p[0]), _r(p[1])] for p in points]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        style = {"fill": fill, "stroke": stroke,
                 "stroke_width_mm": _r(width, 3), "closed": True}
        return self.add(layer, "polygon",
                        _bbox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys),
                              stroke=width),
                        element, panel=panel, evidence=evidence, style=style,
                        points=pts, **extra)

    def path(self, layer, element, points, stroke=FRAME, width=0.3,
             panel=None, evidence=None, dash=None, ptype="path", bbox=None,
             d=None, cubic=None, **extra):
        pts = [[_r(p[0]), _r(p[1])] for p in points]
        if bbox is None:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bbox = _bbox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys),
                         stroke=width)
        style = {"fill": "none", "stroke": stroke,
                 "stroke_width_mm": _r(width, 3), "linecap": "round",
                 "linejoin": "round"}
        if dash:
            style["dash"] = list(dash)
        return self.add(layer, ptype, bbox, element, panel=panel,
                        evidence=evidence, style=style, points=pts, d=d,
                        cubic=cubic, **extra)

    def circle(self, layer, element, cx, cy, r, fill="none", stroke="none",
               width=0.3, panel=None, evidence=None, **extra):
        style = {"fill": fill, "stroke": stroke, "stroke_width_mm": _r(width, 3)}
        return self.add(layer, "circle", _bbox(cx - r, cy - r, 2 * r, 2 * r),
                        element, panel=panel, evidence=evidence, style=style,
                        cx=_r(cx), cy=_r(cy), r=_r(r), **extra)


# --------------------------------------------------------------------------- #
# text metrics used by the planner (must agree with _Builder.text)
# --------------------------------------------------------------------------- #
def _wrap_need(text, size_pt, fs, box_w, leading=1.25, bold=False):
    """(lines, block_height, widest_line) for a box of width `box_w`."""
    lines = measure.wrap_text(text or "", size_pt, fs, box_w, bold)
    lh = measure.line_height_mm(size_pt, leading)
    widest = max([measure.text_width_mm(ln, size_pt, fs, bold) for ln in lines] or [0.0])
    return lines, lh * len(lines), widest


def _shape_of(node):
    shape = node.get("shape") or "rrect"
    return shape if shape in scene.SHAPES else "rrect"


def _inset_x(shape, w):
    if shape in ("hexagon", "diamond"):
        return w * 0.20
    if shape in ("circle", "ellipse"):
        return w * 0.14
    if shape == "note":
        return w * 0.12
    if shape == "capsule":
        return min(w * 0.18, 4.0)
    if shape == "rrect":
        return 1.4
    return 0.8


def _spacing_compactness(level):
    """Spacing ladder (mm) — layout density, not typography.

    Level 0 is comfortable; each step tightens leading/padding so that dense
    panels fit *without* dropping text or breaking the 7 pt type floor.
    """
    steps = [
        {"gap": 1.6, "node_gap_y": 1.4, "node_pad_y": 1.2, "leading": 1.22,
         "label_leading": 1.18, "legend_row": 3.4, "panel_pad": 1.8, "hdr_pad": 1.0},
        {"gap": 1.2, "node_gap_y": 1.0, "node_pad_y": 0.9, "leading": 1.16,
         "label_leading": 1.12, "legend_row": 3.0, "panel_pad": 1.4, "hdr_pad": 0.8},
        {"gap": 0.9, "node_gap_y": 0.7, "node_pad_y": 0.6, "leading": 1.10,
         "label_leading": 1.06, "legend_row": 2.8, "panel_pad": 1.1, "hdr_pad": 0.6},
    ]
    return steps[max(0, min(level, len(steps) - 1))]


def _node_metrics(node, cell_w, fs, sizes):
    """Height a node needs in a cell of width `cell_w`, plus its wrapped lines."""
    sp = sp_of(sizes)
    shape = _shape_of(node)
    inset = _inset_x(shape, cell_w)
    inner_w = max(4.0, cell_w - 2 * inset - 0.4)
    label = str(node.get("label") or "")
    l_lines, l_h, _ = _wrap_need(label, sizes["node_label"], fs, inner_w,
                                 sp["label_leading"], True)
    h = l_h
    s_lines = []
    if node.get("sublabel"):
        s_lines, s_h, _ = _wrap_need(str(node["sublabel"]), sizes["node_sub"], fs,
                                     inner_w, sp["label_leading"])
        h += 0.25 + s_h
    return {"h": h + 2 * sp["node_pad_y"] + 0.3, "inner_w": inner_w,
            "label_lines": l_lines, "sub_lines": s_lines, "disc": 0.0}


def _choose_columns(nodes, area_w, fs, sizes, corr=(0.0, 0.0)):
    """(ncols, required_height) — fewest columns that fit the width budget."""
    cx, cy = corr
    cands = []
    for ncols in range(1, len(nodes) + 1):
        cell_w = (area_w - (ncols - 1) * (NODE_GAP_X + cx)) / ncols
        first = nodes[0]
        if ncols > 1 and cell_w - 2 * _inset_x(_shape_of(first), cell_w) < MIN_NODE_W_MM:
            break
        nrows = int(math.ceil(len(nodes) / float(ncols)))
        total = 0.0
        for r in range(nrows):
            row = nodes[r * ncols:(r + 1) * ncols]
            total += max(max(_node_metrics(n, cell_w, fs, sizes)["h"] for n in row),
                         MIN_NODE_H_MM)
        total += (nrows - 1) * (sp_of(sizes)["node_gap_y"] + cy)
        cands.append((ncols, total))
    if not cands:
        return 1, MIN_NODE_H_MM
    return min(cands, key=lambda c: (c[1], c[0]))


def _panel_header_h(panel, col_w, fs, sizes):
    """Header height measured from the real title wrap (shared by plan+emit)."""
    title = str(panel.get("title") or "")
    _, t_h, _ = _wrap_need(title, sizes["panel_title"], fs,
                           max(10.0, col_w - 2 * PANEL_PAD_MM - 16.0), 1.15, True)
    return max(PANEL_HEADER_MIN_MM, t_h + 2.0)


def _corridors(panel_edges, fs, sizes, area_w, ncols):
    """Free lanes between node cells so edge labels have somewhere to sit.

    Without lanes an edge label plate lands on top of a neighbouring node (the
    single most visible layout defect).  The lane is the measured label size.
    """
    labels = [str(e.get("label")) for e in (panel_edges or []) if isinstance(e, dict)
              and e.get("label")]
    if not labels:
        return 0.0, 0.0
    oz = sizes["edge_label"]
    widest = max(measure.text_width_mm(t, oz, fs) for t in labels) + 1.2
    cy = measure.line_height_mm(oz, 1.15) + 1.4
    cell = area_w / max(1, ncols)
    cx = _clamp(widest, 0.0, max(0.0, cell * 0.55))
    return cx, cy


def _diagram_need(nodes, width, fs, sizes, corr=(0.0, 0.0)):
    """(ncols, height) for the node grid inside `width`."""
    if not nodes:
        return 1, 0.0
    return _choose_columns(nodes, width, fs, sizes, corr)


def _node_grid(nodes, area, ncols, fs, sizes, corr=(0.0, 0.0)):
    """Position node boxes in `area`; the grid never exceeds the area."""
    nrows = int(math.ceil(len(nodes) / float(ncols))) if nodes else 0
    if not nrows:
        return {}
    sp = sp_of(sizes)
    cx, cy = corr
    gap_y = sp["node_gap_y"] + (cy if nrows > 1 else 0.0)
    gap_x = NODE_GAP_X + (cx if ncols > 1 else 0.0)
    cell_w = (area["w"] - (ncols - 1) * gap_x) / ncols
    req = []
    for r in range(nrows):
        row = nodes[r * ncols:(r + 1) * ncols]
        req.append(max(max(_node_metrics(n, cell_w, fs, sizes)["h"] for n in row),
                       MIN_NODE_H_MM))
    span = sum(req) + (nrows - 1) * gap_y
    if span > area["h"] + 1e-9 and nrows > 1:
        # 1st pass: give up the lane space (it is only there to make room for
        # edge labels) before touching the rows, because shrinking a row is
        # what drops a wrapped label line.
        lane_min = sp["node_gap_y"]
        deficit = span - area["h"]
        give = min(deficit, (gap_y - lane_min) * (nrows - 1))
        gap_y -= give / float(nrows - 1)
        span = sum(req) + (nrows - 1) * gap_y
    if span > area["h"] + 1e-9:
        # 2nd pass: clamp rows so a frame or a label never leaves the panel
        room = max(1.0, area["h"] - (nrows - 1) * gap_y)
        factor = room / max(1e-6, sum(req))
        req = [max(1.5, v * factor) for v in req]
        span = sum(req) + (nrows - 1) * gap_y
    extra = max(0.0, area["h"] - span)
    grow_cap = sum(req) * 0.35 / float(nrows) if nrows else 0.0
    grow = min(extra / float(nrows), grow_cap)
    offset = max(0.0, (area["h"] - span - grow * nrows) / 2.0)
    boxes, y = {}, area["y"] + offset
    for r in range(nrows):
        h = req[r] + grow
        for c in range(ncols):
            idx = r * ncols + c
            if idx >= len(nodes):
                break
            x = area["x"] + c * (cell_w + gap_x)
            boxes[nodes[idx].get("id")] = _bbox(x, y, cell_w, h)
        y += h + gap_y
    return boxes


# --------------------------------------------------------------------------- #
# chart sizing
# --------------------------------------------------------------------------- #
def _chart_min_box(cw):
    w = min(max(CHART_MIN_W_MM, 0.44 * cw), cw)
    return w, CHART_MIN_H_MM


def _chart_pref_box(cw, ch):
    return min(max(CHART_MIN_W_MM, 0.46 * cw), cw), min(max(CHART_PREF_H_MM, CHART_MIN_H_MM), ch)


def _split_row(total_w, items):
    """Split `total_w` into len(items) equal columns with a small gap."""
    n = max(1, len(items))
    gap = BAND_GAP_MM
    if n == 1:
        return [total_w]
    each = (total_w - (n - 1) * gap) / n
    return [each] * n


def _stack_boxes(width, available, n):
    """Vertical stack of n equal boxes inside `available` height."""
    if n <= 0:
        return []
    gap = BAND_GAP_MM
    each = (available - (n - 1) * gap) / n
    return [each] * n, gap


# --------------------------------------------------------------------------- #
# panel planning (measure only, no primitives)
# --------------------------------------------------------------------------- #
def _panel_parts(panel):
    nodes = [n for n in (panel.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (panel.get("edges") or []) if isinstance(e, dict)]
    charts = [c for c in (panel.get("charts") or []) if isinstance(c, dict)]
    legend = panel.get("legend") if isinstance(panel.get("legend"), dict) else None
    colorbars = [c for c in (panel.get("colorbars") or []) if isinstance(c, dict)]
    return nodes, edges, charts, legend, colorbars


def _side_band_need(legend, colorbars, sizes, fs):
    """Height needed by a legend/colorbar column."""
    h = 0.0
    if legend and (legend.get("items") or []):
        h += LEGEND_ROW_MM * (len(legend["items"]) + (1.0 if legend.get("title") else 0.0)) + 1.6
    for cb in colorbars:
        h += 20.0 + (3.0 if cb.get("title") else 0.0) + 3.0
    return h


def _cb_need(cb, fs, sizes, max_w):
    """(width, height) a horizontal colour bar needs so its title is not clipped."""
    oz = sizes["colorbar"]
    title = str(cb.get("title") or cb.get("id") or "")
    unit = str(cb.get("unit") or "")
    full = title if not unit else "%s (%s)" % (title, unit)
    words = [w for w in full.split() if w]
    longest = max([measure.text_width_mm(w, oz, fs, True) for w in words] or [8.0])
    one_line = measure.text_width_mm(full, oz, fs, True)
    w = _clamp(max(one_line + 2.0, longest + 2.0), COLORBAR_V_MIN_W, max_w)
    lines, t_h, _ = _wrap_need(full, oz, fs, max(4.0, w - 1.4), 1.15, True)
    h = min(t_h, 2 * measure.line_height_mm(oz, 1.15)) + 4.4 + 3.4 + 0.8
    return w, h, len(lines)


def _legend_row(sizes, fs):
    """Legend row pitch: never smaller than one real line of text."""
    oz = sizes.get("legend", BASE_SIZES["legend"])
    return max(sp_of(sizes)["legend_row"],
               measure.line_height_mm(oz, 1.25) + 0.45)


def _legend_need(legend, items, fs, sizes):
    """(width, height) a legend needs (2 columns when it helps)."""
    n = len(items)
    ncol = 2 if (n >= 3) else 1
    rows = int(math.ceil(n / float(ncol)))
    oz = sizes.get("legend", BASE_SIZES["legend"])
    longest = max([measure.text_width_mm(str(it.get("label") or ""), oz, fs)
                   for it in items] or [8.0])
    row = _legend_row(sizes, fs)
    col = max(LEGEND_COL_W_MM, longest + 5.0 / ncol)
    w = max(col * ncol, longest + 5.0)
    h = (row * rows) + (row if legend.get("title") else 0.0) + 1.0
    return min(w, 90.0), h, ncol


def _plan_panel(panel, content_w, avail_h, fs, sizes):
    """Choose the arrangement that needs the least height (measure only)."""
    nodes, edges, charts, legend, colorbars = _panel_parts(panel)
    items = [it for it in ((legend or {}).get("items") or []) if isinstance(it, dict)]
    corr = _corridors(edges, fs, sizes, content_w, 2)
    has_side = bool(charts or items or colorbars)
    if not has_side:
        ncols, dh = _diagram_need(nodes, content_w, fs, sizes, corr)
        return {"mode": "nodes", "need_h": dh, "ncols": ncols, "diagram_h": dh,
                "corr": corr, "chart_boxes": [], "legend_box": None,
                "cb_boxes": [], "side_w": 0.0}

    cw_chart_min, ch_min = _chart_min_box(content_w)
    leg_w, leg_h = (0.0, 0.0)
    if items:
        leg_w, leg_h, _ncol = _legend_need(legend, items, fs, sizes)
    cb_w, cb_h, _cbl = (0.0, 0.0, 0)
    if colorbars:
        cb_w, cb_h, _cbl = _cb_need(colorbars[0], fs, sizes, max(20.0, content_w * 0.55))

    # ---- STACK: nodes on top, chart band underneath, legend+colorbar right -- #
    right_w = max(leg_w, cb_w)
    right_need = 0.0
    if items:
        right_need += leg_h
    if colorbars:
        right_need += (BAND_GAP_MM if right_need else 0.0) + cb_h
    band_h = max(ch_min if charts else 0.0, right_need)
    chart_span = max(0.0, content_w - (right_w + BAND_GAP_MM if right_w else 0.0))
    n_row = len(charts) or 1
    while n_row > 1 and chart_span / n_row < CHART_MIN_W_MM:
        n_row -= 1
    rows_c = int(math.ceil(len(charts) / float(n_row))) if charts else 0
    if charts and n_row < len(charts):
        band_h = max(band_h, ch_min * rows_c + (rows_c - 1) * BAND_GAP_MM)
    ncols_s, dh_s = _diagram_need(nodes, content_w, fs, sizes, corr)
    need_stack = dh_s + ((BAND_GAP_MM + band_h) if band_h else 0.0)

    # ---- SIDE: nodes left, chart(s)/legend/colorbar right ------------------ #
    side_w = 0.0
    if charts:
        side_w = max(side_w, cw_chart_min)
    side_w = max(side_w, max(leg_w, cb_w))
    side_w = min(side_w, max(0.0, content_w - MIN_NODE_W_MM - BAND_GAP_MM))
    left_w = max(MIN_NODE_W_MM, content_w - side_w - BAND_GAP_MM)
    ncols_d, dh_d = _diagram_need(nodes, left_w, fs, sizes, corr)
    right_need_s = 0.0
    if charts:
        right_need_s += ch_min * len(charts) + BAND_GAP_MM * max(0, len(charts) - 1)
    if items:
        right_need_s += (BAND_GAP_MM if right_need_s else 0.0) + leg_h
    if colorbars:
        right_need_s += (BAND_GAP_MM if right_need_s else 0.0) + cb_h
    need_side = max(dh_d, right_need_s) if nodes else right_need_s

    if need_stack <= need_side + 1e-9:
        return {"mode": "stack", "need_h": need_stack, "ncols": ncols_s, "corr": corr,
                "diagram_h": dh_s, "band_h": band_h, "chart_span": max(CHART_MIN_W_MM, chart_span),
                "n_charts_row": n_row, "right_w": right_w, "leg_h": leg_h,
                "cb_h": cb_h, "leg_w": leg_w, "cb_w": cb_w,
                "chart_boxes": [], "legend_box": None, "cb_boxes": [], "side_w": 0.0}
    return {"mode": "side", "need_h": need_side, "ncols": ncols_d, "corr": corr,
            "diagram_h": dh_d, "band_h": 0.0, "side_w": side_w, "left_w": left_w,
            "leg_h": leg_h, "cb_h": cb_h, "leg_w": leg_w, "cb_w": cb_w,
            "chart_boxes": [], "legend_box": None, "cb_boxes": []}


def _plan_rows(spec, fs, sizes, inner, y0, avail_h):
    """Measure every panel, group into rows, return row heights."""
    panels = _ordered_panels(spec)
    layout_meta = (spec.get("meta") or {}).get("layout")
    columns = 2
    if isinstance(layout_meta, dict) and isinstance(layout_meta.get("columns"), int):
        columns = max(1, layout_meta["columns"])
    columns = max(1, min(columns, max(1, len(panels))))
    rows = int(math.ceil(len(panels) / float(columns))) if panels else 0
    col_w = (inner["w"] - (columns - 1) * GUTTER_MM) / columns if columns else inner["w"]
    plans = []
    for panel in panels:
        plans.append(_plan_panel(panel, col_w - 2 * PANEL_PAD_MM,
                                 avail_h, fs, sizes))
    row_heights = []
    for r in range(rows):
        chunk = ["plans"] and plans[r * columns:(r + 1) * columns]
        hdr = max([_panel_header_h(panels[r * columns + i], col_w, fs, sizes)
                   for i in range(len(chunk))] or [0.0])
        row_heights.append(max([p["need_h"] for p in chunk] or [0.0]) + hdr + 2.0)
    return {"panels": panels, "columns": columns, "rows": rows, "col_w": col_w,
            "plans": plans, "row_heights": row_heights}


def _ordered_panels(spec):
    panels = [p for p in (spec.get("panels") or []) if isinstance(p, dict)]
    layout_meta = (spec.get("meta") or {}).get("layout")
    roles = list(scene.PANEL_ROLES)
    if isinstance(layout_meta, dict) and isinstance(layout_meta.get("row_order"), list):
        roles = [r for r in layout_meta["row_order"] if isinstance(r, str)] or roles
    ordered, used = [], set()
    for role in roles:
        for i, p in enumerate(panels):
            if i not in used and p.get("role") == role:
                ordered.append(p)
                used.add(i)
    for i, p in enumerate(panels):
        if i not in used:
            ordered.append(p)
            used.add(i)
    return ordered


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #
def layout(spec, fs):
    """Build the scene. Deterministic; never raises on an odd (but valid) spec."""
    meta = spec.get("meta") or {}
    canvas = scene.resolve_canvas(meta)
    W, H = float(canvas["w_mm"]), float(canvas["h_mm"])
    M = float(canvas["margin_mm"])
    inner = _bbox(M, M, W - 2 * M, H - 2 * M)
    base = base_sizes(spec)              # preset typography, clamped to floors

    best = None
    for level in range(len(SPACING_LEVELS)):
        scale = 1.0
        for _ in range(14):
            sizes = scaled_sizes(scale, level, base)
            bands = _measure_bands(spec, fs, sizes, inner)
            avail = inner["h"] - bands["title_h"] - bands["caption_h"] - bands["gaps"]
            rows_plan = _plan_rows(spec, fs, sizes, inner, inner["y"] + bands["title_h"], avail)
            total = sum(rows_plan["row_heights"]) + max(0, rows_plan["rows"] - 1) * GUTTER_MM
            candidate = {"sizes": sizes, "bands": bands, "rows": rows_plan,
                         "avail": avail, "total": total, "scale": scale, "level": level}
            if best is None or total < best["total"] - 1e-9:
                best = candidate
            if total <= avail + 1e-6:
                best = candidate
                break
            if scale <= 0.55:
                break
            shrink = _clamp(math.sqrt(max(0.25, avail / max(total, 1e-6))), 0.80, 0.98)
            scale = round(max(0.55, scale * shrink), 3)
        if best is not None and best["total"] <= best["avail"] + 1e-6:
            break

    # hard clamp: the panel grid may never leave the margin box (gate A)
    rows_plan = best["rows"]
    total = best["total"]
    if total > best["avail"] + 1e-6:
        room = max(4.0, best["avail"] - max(0, rows_plan["rows"] - 1) * GUTTER_MM)
        factor = room / max(1e-6, sum(rows_plan["row_heights"]))
        rows_plan["row_heights"] = [max(4.0, h * factor) for h in rows_plan["row_heights"]]
        best["clamped"] = True
    sizes = best["sizes"]
    bands = best["bands"]
    rows_plan = best["rows"]
    b = _Builder(spec, fs, sizes)

    # page background covers the full page (bleed is legitimate; see SCENE.md)
    b.add("frame", "rect", _bbox(0, 0, W, H), "page-background",
          style={"fill": PAGE_BG, "stroke": "none", "stroke_width_mm": 0.0},
          bleed=True)

    # ---- title band ------------------------------------------------------- #
    y = inner["y"]
    b.text("annotation", "figure-title", bands["title"], inner["x"], y,
           inner["w"], bands["title_h"], "title", bold=True, color=INK,
           valign="top", leading=1.12)
    if bands["subtitle"]:
        b.text("annotation", "figure-subtitle", bands["subtitle"], inner["x"],
               y + bands["title_block_h"] + 0.8, inner["w"],
               bands["subtitle_h"], "subtitle", color=MUTED, valign="top",
               leading=1.2)
    y += bands["title_h"] + bands["gaps"] / 2.0

    # ---- caption / conclusion strip (bottom) ------------------------------ #
    if bands["caption_h"]:
        cap_y = inner["y"] + inner["h"] - bands["caption_h"]
        b.line("annotation", "caption-rule", (inner["x"] + 0.4, cap_y - 1.2),
               (inner["x"] + inner["w"] - 0.4, cap_y - 1.2), stroke=GRID, width=0.3)
        cy = cap_y
        if bands["caption"]:
            b.text("annotation", "figure-caption", bands["caption"], inner["x"], cy,
                   inner["w"], bands["caption_text_h"], "caption", color=INK,
                   valign="top", leading=1.22)
            cy += bands["caption_text_h"] + 0.4
        if bands["conclusion"]:
            b.text("annotation", "figure-conclusion", bands["conclusion"], inner["x"],
                   cy, inner["w"], max(1.5, inner["y"] + inner["h"] - cy),
                   "caption", color=INK, valign="top", leading=1.22)

    # ---- panels ----------------------------------------------------------- #
    columns = rows_plan["columns"]
    col_w = rows_plan["col_w"]
    panel_records = []
    cur_y = y
    plabel_style = meta.get("panel_label_style") \
        if isinstance(meta.get("panel_label_style"), dict) else {}
    for r, row_h in enumerate(rows_plan["row_heights"]):
        for c in range(columns):
            idx = r * columns + c
            if idx >= len(rows_plan["panels"]):
                break
            panel = rows_plan["panels"][idx]
            box = _bbox(inner["x"] + c * (col_w + GUTTER_MM), cur_y, col_w, row_h)
            rec = _emit_panel(b, panel, box, idx, fs, rows_plan["plans"][idx],
                              letter=_panel_letter(idx, plabel_style),
                              plabel_style=plabel_style)
            panel_records.append(rec)
        cur_y += row_h + GUTTER_MM

    if best.get("clamped") or best["total"] > best["avail"] + 1e-6:
        b.warn("W_LAYOUT_DENSE",
               "panels measured %.1f mm but only %.1f mm is available "
               "(type scale %.2f, spacing level %d); row heights were clamped so "
               "nothing leaves the page" % (best["total"], best["avail"],
                                            best["scale"], best.get("level", 0)))

    _maybe_scalebar(b, spec, panel_records)

    # ---- glyph coverage --------------------------------------------------- #
    blob = " ".join(" ".join(p.get("lines") or []) for p in b.primitives
                    if p.get("type") == "text")
    missing = _check_glyphs(b, blob)
    if missing:
        b.warn("W_GLYPH_MISSING",
               "%d character(s) have no glyph in any resolved face: %s"
               % (len(missing), "".join(missing[:24])))

    prims = sorted(b.primitives, key=lambda p: (
        LAYER_ORDER.index(p["layer"]) if p["layer"] in LAYER_ORDER else 99,
        0 if p.get("type") != "text" else 1))

    return {
        "canvas": {"w_mm": _r(W), "h_mm": _r(H), "dpi": int(canvas["dpi"]),
                   "margin_mm": _r(M)},
        "primitives": prims,
        "panels": panel_records,
        "marks": b.marks,
        "warnings": b.warnings,
        "meta": {
            "generator": GENERATOR,
            "version": VERSION,
            "case_id": meta.get("case_id"),
            "title": bands["title"],
            "language": meta.get("language", "auto"),
            "palette": list(b.palette),
            "palette_id": (spec.get("palette") or {}).get("id", "okabe-ito"),
            "preset": meta.get("preset"),
            "stroke_widths_mm": dict(b.widths),
            "spec_sha256": scene.sha256_text(_canonical_spec(spec)),
            "layers": list(LAYER_ORDER),
            "font": getattr(fs, "family_css", "sans-serif"),
            "units": "mm, origin top-left, y down",
            "type_scale": best["scale"],
            "spacing_level": best.get("level", 0),
            "font_sizes_pt": {k: v for k, v in sizes.items() if not k.startswith("__")},
            "grid": {"columns": columns, "rows": rows_plan["rows"],
                     "row_heights_mm": [_r(v) for v in rows_plan["row_heights"]]},
        },
    }


def _panel_letter(index, style=None):
    """Multi-panel label a, b, c ... (A, B, C by preset style)."""
    if index < 26:
        ch = chr(ord("a") + index)
    else:
        ch = "p%d" % (index + 1)
    case = ((style or {}).get("case") or "lower").lower()
    return ch.upper() if case.startswith("u") else ch


def _canonical_spec(spec):
    import json
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


TITLE_LEADING = 1.15


def _fit_title(text, size_pt, fs, max_w, max_lines=2):
    """Fit a panel title: wrap, then ellipsize at a word boundary."""
    lines = measure.wrap_text(text, size_pt, fs, max_w, True)
    if len(lines) <= max_lines:
        lh = measure.line_height_mm(size_pt, TITLE_LEADING)
        return text, lines, lh * max(1, len(lines))
    kept = lines[:max_lines]
    last = kept[-1]
    while last and measure.text_width_mm(last + "\u2026", size_pt, fs) > max_w:
        cut = last.rfind(" ")
        last = last[:cut] if cut > 0 else last[:-1]
    kept[-1] = (last.rstrip() + "\u2026")
    lh = measure.line_height_mm(size_pt, TITLE_LEADING)
    return " ".join(kept), kept, lh * max_lines


def _measure_bands(spec, fs, sizes, inner):
    meta = spec.get("meta") or {}
    texts = spec.get("texts") or {}
    title = meta.get("title") or texts.get("title") or (meta.get("case_id") or "")
    subtitle = meta.get("subtitle") or texts.get("subtitle") or ""
    t_lines, t_h, _ = _wrap_need(title, sizes["title"], fs, inner["w"], 1.12, True)
    t_h = min(t_h, 0.18 * inner["h"])
    s_lines, s_h, _ = _wrap_need(subtitle, sizes["subtitle"], fs, inner["w"], 1.2) \
        if subtitle else ([], 0.0, 0.0)
    s_h = min(s_h, 0.10 * inner["h"])
    title_block = t_h + (0.8 + s_h if subtitle else 0.0)

    caption = texts.get("caption") or ""
    conclusion = texts.get("conclusion") or ""
    cap_h = 0.0
    cap_text_h = 0.0
    conc_h = 0.0
    if caption:
        _, cap_text_h, _ = _wrap_need(str(caption), sizes["caption"], fs, inner["w"], 1.22)
    if conclusion:
        _, conc_h, _ = _wrap_need(str(conclusion), sizes["caption"], fs, inner["w"], 1.22)
    need_caption = cap_text_h + (0.4 + conc_h if conclusion else 0.0) + 1.6
    cap_h = min(need_caption, 0.20 * inner["h"]) if (caption or conclusion) else 0.0
    if cap_h:
        # the two blocks share the strip; give each its measured share
        if caption and conclusion:
            share = cap_text_h / max(1e-6, cap_text_h + conc_h)
            cap_text_h = max(1.5, (cap_h - 1.6) * share)
            conc_h = max(1.5, (cap_h - 1.6) - cap_text_h - 0.4)
        elif caption:
            cap_text_h = max(1.5, cap_h - 1.6)
        else:
            conc_h = max(1.5, cap_h - 1.6)

    gaps = 4.0 if cap_h else 2.0
    return {"title": title, "subtitle": subtitle, "title_h": title_block,
            "title_block_h": t_h, "subtitle_h": s_h, "caption": caption,
            "conclusion": conclusion, "caption_h": cap_h,
            "caption_text_h": cap_text_h, "conclusion_h": conc_h, "gaps": gaps}


def _check_glyphs(b, text):
    if not text.strip():
        return []
    fs = b.fs
    missing, seen = [], set()
    for ch in text:
        if ch.isspace() or ch in seen:
            continue
        seen.add(ch)
        ok = False
        for p in fs.all_paths():
            try:
                if measure.char_supported(p, ord(ch)):
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            missing.append(ch)
    if missing:
        try:
            fs.record_missing(missing)
        except Exception:
            pass
    return sorted(missing)


# --------------------------------------------------------------------------- #
# panel emission
# --------------------------------------------------------------------------- #
def _emit_panel(b, panel, box, index, fs, plan, letter=None, plabel_style=None):
    pid = panel.get("id")
    role = panel.get("role")
    nodes, edges, charts, legend, colorbars = _panel_parts(panel)
    sizes = b.sizes

    # header: measured so the title never falls below its floor
    sp = sp_of(sizes)
    pad = sp["panel_pad"]
    hdr_pad = sp["hdr_pad"]
    role_tag = str(role or "").upper()
    tag_w = measure.text_width_mm(role_tag, sizes["panel_role"], fs) + 1.4
    letter = str(letter) if letter else None
    letter_pt = ((plabel_style or {}).get("size_pt") if isinstance(plabel_style, dict) else None)
    letter_pt = float(letter_pt) if isinstance(letter_pt, (int, float)) and not isinstance(letter_pt, bool) else sizes["panel_title"]
    letter_pt = max(letter_pt, sizes["panel_role"])
    letter_w = (measure.text_width_mm(letter, letter_pt, fs, True) + 1.8) if letter else 0.0
    panel_title = str(panel.get("title") or panel.get("name") or pid or "")
    title_w = max(12.0, box["w"] - 2 * pad - tag_w - 1.4 - letter_w)
    title, t_lines, t_h = _fit_title(panel_title, sizes["panel_title"], fs, title_w, 2)
    header_h = max(PANEL_HEADER_MIN_MM, t_h + 2 * max(hdr_pad, 1.0))
    b.rect("panel", "panel-frame", box, fill="#FCFCFC", stroke=FRAME, width=0.4,
           rx=1.2, panel=pid)
    header = _bbox(box["x"], box["y"], box["w"], header_h)
    b.rect("panel", "panel-header", header, fill="#EFEFEF", stroke=FRAME,
           width=0.3, panel=pid)
    if letter:
        # multi-panel label (a / b / c ...), aligned across the grid because every
        # panel uses the same header geometry
        b.text("panel", "panel-label", letter, header["x"] + pad,
               header["y"] + hdr_pad, letter_w, max(2.4, header["h"] - 2 * hdr_pad),
               letter_pt, bold=True, color=INK, valign="top", panel=pid,
               max_lines=1, allow_truncate=False)
    b.text("panel", "panel-title", title, header["x"] + pad + letter_w,
           header["y"] + hdr_pad, title_w, t_h + 0.25, "panel_title", bold=True,
           color=INK, valign="top", panel=pid, allow_truncate=False,
           leading=TITLE_LEADING)
    b.text("panel", "panel-role", role_tag,
           header["x"] + header["w"] - pad - tag_w, header["y"] + hdr_pad,
           tag_w, max(2.4, header["h"] - 2 * hdr_pad), "panel_role", color=MUTED,
           align="right", valign="top", panel=pid, max_lines=1, allow_truncate=False)

    content = _bbox(box["x"] + pad, box["y"] + header_h + 0.8,
                    box["w"] - 2 * pad, max(2.0, box["h"] - header_h - 1.6))

    cb = colorbars[0] if colorbars else None
    node_boxes = {}
    if plan["mode"] == "side":
        gap = sp["gap"]
        left = _bbox(content["x"], content["y"], plan["left_w"], content["h"])
        right = _bbox(content["x"] + plan["left_w"] + gap, content["y"],
                      plan["side_w"], content["h"])
        if nodes:
            node_boxes = _node_grid(nodes, left, plan["ncols"], fs, sizes,
                                    plan.get("corr", (0.0, 0.0)))
        _emit_side_column(b, pid, right, charts, legend, colorbars, fs, sizes, cb)
    elif plan["mode"] == "stack":
        gap = sp["gap"]
        band_h = min(plan["band_h"], max(6.0, content["h"] - MIN_DIAGRAM_MM - gap))
        diagram = _bbox(content["x"], content["y"], content["w"],
                        max(4.0, content["h"] - band_h - gap))
        if nodes:
            node_boxes = _node_grid(nodes, diagram, plan["ncols"], fs, sizes,
                                    plan.get("corr", (0.0, 0.0)))
        band = _bbox(content["x"], content["y"] + diagram["h"] + gap,
                     content["w"], max(4.0, content["h"] - diagram["h"] - gap))
        _emit_band(b, pid, band, charts, legend, colorbars, fs, sizes, cb,
                   plan.get("n_charts_row") or len(charts))
    else:
        if nodes:
            node_boxes = _node_grid(nodes, content, plan["ncols"], fs, sizes,
                                    plan.get("corr", (0.0, 0.0)))
    if colorbars and plan["mode"] == "nodes":
        _emit_colorbar(b, pid, colorbars[0], _inset(content, 0.5, 0.5), fs, sizes)

    for node in nodes:
        nbox = node_boxes.get(node.get("id"))
        if nbox is not None:
            _emit_node(b, pid, node, nbox, fs, sizes, cb)

    if edges and node_boxes:
        _emit_edges(b, pid, edges, node_boxes, content, fs, sizes)
    if not nodes and not charts and not legend and not colorbars:
        b.warn("W_EMPTY_PANEL", "panel %s has nothing to draw" % pid, panel=pid)

    return {"id": pid, "role": role, "title": panel.get("title"), "index": index,
            "bbox": box, "header_bbox": header, "content_bbox": content,
            "mode": plan["mode"], "node_count": len(nodes),
            "edge_count": len(edges), "chart_count": len(charts),
            "node_boxes": {k: dict(v) for k, v in sorted(node_boxes.items())},
            "layer": "panel"}


def _inset(box, dx, dy):
    return _bbox(box["x"] + dx, box["y"] + dy, max(0.5, box["w"] - 2 * dx),
                 max(0.5, box["h"] - 2 * dy))


def _emit_band(b, pid, band, charts, legend, colorbars, fs, sizes, cb, n_charts_row):
    """Chart band under the node grid; legend and colour bar stacked at the right."""
    items = [it for it in ((legend or {}).get("items") or []) if isinstance(it, dict)]
    plan_leg_w, plan_leg_h, ncol = (0.0, 0.0, 1)
    if items:
        plan_leg_w, plan_leg_h, ncol = _legend_need(legend, items, fs, sizes)
    plan_cb_w, plan_cb_h, _ = (0.0, 0.0, 0)
    if colorbars:
        plan_cb_w, plan_cb_h, _ = _cb_need(colorbars[0], fs, sizes, max(20.0, band["w"] * 0.55))
    right_w = min(max(plan_leg_w, plan_cb_w), band["w"] * 0.6)
    chart_w = max(CHART_MIN_W_MM, band["w"] - (right_w + BAND_GAP_MM if right_w else 0.0))
    if charts:
        n = max(1, min(n_charts_row or len(charts), len(charts)))
        while n > 1 and chart_w / n < CHART_MIN_W_MM:
            n -= 1
        rows = int(math.ceil(len(charts) / float(n)))
        col_w = (chart_w - (n - 1) * BAND_GAP_MM) / n
        rows = max(1, rows)
        row_h = (band["h"] - (rows - 1) * BAND_GAP_MM) / rows
        for i, chart in enumerate(charts):
            c, r = i % n, i // n
            box = _bbox(band["x"] + c * (col_w + BAND_GAP_MM),
                        band["y"] + r * (row_h + BAND_GAP_MM), col_w, max(6.0, row_h))
            _emit_chart(b, pid, chart, box, fs, sizes, legend, colorbars)
    if not right_w:
        return
    x = band["x"] + band["w"] - right_w
    y = band["y"]
    if items:
        lh = min(plan_leg_h, band["h"])
        _emit_legend(b, pid, legend, _bbox(x, y, right_w, lh), fs, sizes, ncol)
        y += lh + BAND_GAP_MM
    if colorbars:
        ch = min(plan_cb_h, max(6.0, band["y"] + band["h"] - y))
        _emit_colorbar(b, pid, colorbars[0], _bbox(x, y, right_w, ch), fs, sizes)


def _emit_side_column(b, pid, right, charts, legend, colorbars, fs, sizes, cb):
    items = [it for it in ((legend or {}).get("items") or []) if isinstance(it, dict)]
    _lw, leg_h, ncol = _legend_need(legend, items, fs, sizes) if items else (0.0, 0.0, 1)
    _cw, cb_h, _x = _cb_need(colorbars[0], fs, sizes, right["w"]) if colorbars else (0.0, 0.0, 0)
    leg_h = min(leg_h, right["h"] * 0.4)
    cb_h = min(cb_h, right["h"] * 0.4)
    reserved = (leg_h + (BAND_GAP_MM if items else 0.0) +
                cb_h + (BAND_GAP_MM if colorbars else 0.0))
    charts_total = max(0.0, right["h"] - reserved)
    y = right["y"]
    if charts and charts_total > 8.0:
        each = (charts_total - (len(charts) - 1) * BAND_GAP_MM) / len(charts)
        for chart in charts:
            _emit_chart(b, pid, chart, _bbox(right["x"], y, right["w"], max(6.0, each)),
                        fs, sizes, legend, colorbars)
            y += each + BAND_GAP_MM
    elif charts:
        _emit_chart(b, pid, charts[0], _bbox(right["x"], y, right["w"], right["h"] * 0.5),
                    fs, sizes, legend, colorbars)
        y += right["h"] * 0.5 + BAND_GAP_MM
    if items and leg_h > 0:
        _emit_legend(b, pid, legend, _bbox(right["x"], y, right["w"], leg_h), fs,
                     sizes, ncol)
        y += leg_h + BAND_GAP_MM
    if colorbars:
        _emit_colorbar(b, pid, colorbars[0],
                       _bbox(right["x"], y, right["w"], max(8.0, cb_h)), fs, sizes)


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #
def _shape_points(shape, b):
    x, y, w, h = b["x"], b["y"], b["w"], b["h"]
    if shape == "hexagon":
        cut = min(w * 0.18, h * 0.5)
        return [[x + cut, y], [x + w - cut, y], [x + w, y + h / 2.0],
                [x + w - cut, y + h], [x + cut, y + h], [x, y + h / 2.0]]
    if shape == "diamond":
        return [[x + w / 2.0, y], [x + w, y + h / 2.0],
                [x + w / 2.0, y + h], [x, y + h / 2.0]]
    if shape == "note":
        fold = min(w * 0.18, h * 0.45)
        return [[x, y], [x + w - fold, y], [x + w, y + fold], [x + w, y + h],
                [x, y + h]]
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def _emit_nodes_only(b, pid, nodes, boxes, fs, sizes, cb):
    for node in nodes:
        box = boxes.get(node.get("id"))
        if box is not None:
            _emit_node(b, pid, node, box, fs, sizes, cb)


def _emit_node(b, pid, node, box, fs, sizes, cb):
    nid = node.get("id")
    shape = _shape_of(node)
    ev = b.evidence_for(node)
    val = node.get("value")
    fill = NEUTRAL_FILL
    if isinstance(val, (int, float)) and not isinstance(val, bool) and cb:
        vmin, vmax = float(cb.get("vmin", 0.0)), float(cb.get("vmax", 1.0))
        t = (float(val) - vmin) / (vmax - vmin) if vmax > vmin else 0.0
        fill = cmap_color(cb.get("cmap"), t)
    ink = ink_on(fill)
    stroke = FRAME
    common = dict(panel=pid, evidence=ev, node_id=nid, shape=shape,
                  value=val if isinstance(val, (int, float)) and not isinstance(val, bool) else None,
                  unit=node.get("unit"), fill=fill)

    if shape in ("circle", "ellipse"):
        d = min(box["h"] * 0.36, box["w"] * 0.22, 9.0)
        cx, cy = box["x"] + 1.2 + d, box["y"] + box["h"] / 2.0
        common_s = {k: v for k, v in common.items() if k != "fill"}
        if shape == "circle":
            b.circle("entity", "node-shape", cx, cy, d, fill=fill, stroke=stroke,
                     width=0.45, **common_s)
        else:
            b.add("entity", "ellipse",
                  _bbox(cx - d, cy - d * 0.86, 2 * d, 2 * d * 0.86), "node-shape",
                  panel=pid, evidence=ev,
                  style={"fill": fill, "stroke": stroke, "stroke_width_mm": 0.45},
                  cx=_r(cx), cy=_r(cy), rx=_r(d), ry=_r(d * 0.86), node_id=nid,
                  shape=shape, fill=fill, unit=node.get("unit"))
    elif shape in ("hexagon", "diamond", "note"):
        b.polygon("entity", "node-shape", _shape_points(shape, box), fill=fill,
                  stroke=stroke, width=0.45,
                  **{k: v for k, v in common.items() if k != "fill"})
    else:
        rx = None
        if shape == "capsule":
            rx = min(box["h"] / 2.0, box["w"] / 2.0)
        elif shape == "rrect":
            rx = 1.0
        b.rect("entity", "node-shape", box, fill=fill, stroke=stroke, width=0.45,
               rx=rx, **{k: v for k, v in common.items() if k != "fill"})

    sp = sp_of(sizes)
    ins = sp["node_pad_y"]
    inset = _inset_x(shape, box["w"])
    left_extra = 0.0
    if shape in ("circle", "ellipse"):
        d = min(box["h"] * 0.36, box["w"] * 0.22, 9.0)
        left_extra = 2 * d + 1.4
        inset = 1.0
    inner = _bbox(box["x"] + inset + left_extra, box["y"] + ins,
                  max(2.0, box["w"] - 2 * inset - left_extra),
                  max(1.5, box["h"] - 2 * ins))
    lead = sp["label_leading"]
    lh_l = measure.line_height_mm(sizes["node_label"], lead)
    lh_s = measure.line_height_mm(sizes["node_sub"], lead)
    sub = node.get("sublabel")
    label = str(node.get("label") or "")
    l_lines, l_h, _ = _wrap_need(label, sizes["node_label"], fs, inner["w"], lead, True)
    s_lines, s_h = [], 0.0
    if sub:
        s_lines, s_h, _ = _wrap_need(str(sub), sizes["node_sub"], fs, inner["w"], lead)
    avail = inner["h"]
    gap = 0.25
    if s_lines:
        if l_h + gap + s_h <= avail + 1e-9:
            l_box, s_box = l_h, s_h
        else:
            l_box = min(l_h, max(lh_l, avail - gap - lh_s))
            s_box = avail - gap - l_box
            if s_box < lh_s:
                s_box = 0.0
                l_box = avail
        top = inner["y"] + max(0.0, (avail - (l_box + (gap + s_box if s_box else 0.0))) / 2.0)
        b.text("entity", "node-label", label, inner["x"], top, inner["w"], l_box + 0.1,
               "node_label", bold=True, color=ink, align="center", panel=pid,
               evidence=ev, background=fill, leading=lead, allow_truncate=True)
        if s_box:
            b.text("entity", "node-sublabel", str(sub), inner["x"], top + l_box + gap,
                   inner["w"], s_box + 0.1, "node_sub", color=ink, align="center",
                   panel=pid, evidence=ev, background=fill, leading=lead,
                   allow_truncate=True)
    else:
        b.text("entity", "node-label", label, inner["x"], inner["y"], inner["w"],
               inner["h"], "node_label", bold=True, color=ink, align="center",
               valign="middle", panel=pid, evidence=ev, background=fill,
               leading=lead, allow_truncate=True)


# --------------------------------------------------------------------------- #
# edges (typed as arrows, one primitive per spec edge)
# --------------------------------------------------------------------------- #
def _side_of(src, dst):
    scx, scy = src["x"] + src["w"] / 2.0, src["y"] + src["h"] / 2.0
    dcx, dcy = dst["x"] + dst["w"] / 2.0, dst["y"] + dst["h"] / 2.0
    dx, dy = dcx - scx, dcy - scy
    if abs(dx) >= abs(dy):
        return ("right" if dx >= 0 else "left"), ("left" if dx >= 0 else "right")
    return ("bottom" if dy >= 0 else "top"), ("top" if dy >= 0 else "bottom")


def _anchor(box, side, frac):
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    if side == "right":
        return (x + w, y + h * frac)
    if side == "left":
        return (x, y + h * frac)
    if side == "bottom":
        return (x + w * frac, y + h)
    return (x + w * frac, y)


def _orthogonal(p0, out_side, p1, in_side):
    h_out = out_side in ("left", "right")
    h_in = in_side in ("left", "right")
    if h_out and h_in:
        midx = (p0[0] + p1[0]) / 2.0
        return [p0, (midx, p0[1]), (midx, p1[1]), p1]
    if (not h_out) and (not h_in):
        midy = (p0[1] + p1[1]) / 2.0
        return [p0, (p0[0], midy), (p1[0], midy), p1]
    if h_out and not h_in:
        return [p0, (p1[0], p0[1]), p1]
    return [p0, (p0[0], p1[1]), p1]


def _emit_edges(b, pid, edges, boxes, area, fs, sizes):
    groups = {}
    routed = []
    for ei, edge in enumerate(edges):
        src, dst = boxes.get(edge.get("from")), boxes.get(edge.get("to"))
        if src is None or dst is None:
            b.warn("W_EDGE_DANGLING", "edge %s references a node missing from the "
                   "layout" % edge.get("id"), panel=pid)
            continue
        out_side, in_side = _side_of(src, dst)
        groups.setdefault((edge.get("from"), out_side), []).append(ei)
        groups.setdefault((edge.get("to"), in_side), []).append(ei)
        routed.append((ei, edge, src, dst, out_side, in_side))

    def frac_for(node_id, side, ei):
        members = groups.get((node_id, side)) or [ei]
        m = len(members)
        if m <= 1:
            return 0.5
        k = members.index(ei) if ei in members else 0
        span = min(0.36, 0.13 * (m - 1))
        return (0.5 - span) + (2 * span) * k / float(m - 1)

    for ei, edge, src, dst, out_side, in_side in routed:
        kind = edge.get("kind") or "flow"
        dash = DASHES.get(kind, [])
        ev = b.evidence_for(edge)
        p0 = _anchor(src, out_side, frac_for(edge.get("from"), out_side, ei))
        p1 = _anchor(dst, in_side, frac_for(edge.get("to"), in_side, ei))
        attrs = dict(edge_id=edge.get("id"), edge_kind=kind, kind=kind,
                     **{"from": edge.get("from"), "to": edge.get("to")})
        if kind == "feedback":
            ctrl = _feedback_controls(p0, p1, out_side)
            nums = [float(v) for v in ctrl.split()]
            pts = [p0] + _flat_cubic(p0, (nums[0], nums[1]), (nums[2], nums[3]), p1)
            xs = [p[0] for p in pts] + [p1[0]]
            ys = [p[1] for p in pts] + [p1[1]]
            b.path("relation", "edge-line", pts + [p1], stroke=FRAME, width=EDGE_W_MM,
                   panel=pid, evidence=ev, dash=dash, ptype="arrow",
                   bbox=_bbox(min(xs) - 0.5, min(ys) - 0.5,
                              max(xs) - min(xs) + 1.0, max(ys) - min(ys) + 1.0),
                   d="M %s %s C %s %s %s %s %s"
                   % (_r(p0[0]), _r(p0[1]), *[_r(v) for v in nums], _r(p1[0]), _r(p1[1])),
                   cubic=[[nums[0], nums[1]], [nums[2], nums[3]]], **attrs)
            tip_prev = pts[-1] if len(pts) > 1 else p0
        else:
            pts = _orthogonal(p0, out_side, p1, in_side)
            b.path("relation", "edge-line", pts, stroke=FRAME, width=EDGE_W_MM,
                   panel=pid, evidence=ev, dash=dash, ptype="arrow", **attrs)
            tip_prev = pts[-2]
        tip = p1
        dirv = (tip[0] - tip_prev[0], tip[1] - tip_prev[1])
        if abs(dirv[0]) < 1e-9 and abs(dirv[1]) < 1e-9:
            dirv = (1.0, 0.0)
        if kind == "inhibition":
            _emit_tbar(b, pid, edge, tip, dirv, ev)
        else:
            _emit_arrowhead(b, pid, edge, tip, dirv, ev, kind)
        if edge.get("label"):
            _emit_edge_label(b, pid, edge, pts, area, fs, sizes, ev, boxes)


def _flat_cubic(p0, c1, c2, p1, steps=18):
    out = []
    for i in range(1, steps):
        t = i / float(steps)
        mt = 1.0 - t
        out.append(((mt ** 3) * p0[0] + 3 * (mt ** 2) * t * c1[0] +
                    3 * mt * (t ** 2) * c2[0] + (t ** 3) * p1[0],
                    (mt ** 3) * p0[1] + 3 * (mt ** 2) * t * c1[1] +
                    3 * mt * (t ** 2) * c2[1] + (t ** 3) * p1[1]))
    return out


def _feedback_controls(p0, p1, out_side):
    if out_side in ("left", "right"):
        bulge = 9.0 if out_side == "right" else -9.0
        c1 = (p0[0] + bulge, p0[1] - 4.0)
        c2 = (p1[0] - bulge, p1[1] - 4.0)
    else:
        bulge = 7.0 if out_side == "bottom" else -7.0
        c1 = (p0[0] - 4.0, p0[1] + bulge)
        c2 = (p1[0] - 4.0, p1[1] - bulge)
    return "%s %s %s %s %s %s" % (_r(c1[0]), _r(c1[1]), _r(c2[0]), _r(c2[1]),
                                  _r(p1[0]), _r(p1[1]))


def _emit_arrowhead(b, pid, edge, tip, dirv, ev, kind):
    L = math.hypot(dirv[0], dirv[1]) or 1.0
    ux, uy = dirv[0] / L, dirv[1] / L
    px, py = -uy, ux
    ax, ay = tip[0] - ux * EDGE_HEAD_MM, tip[1] - uy * EDGE_HEAD_MM
    half = EDGE_HEAD_MM * 0.45
    b.polygon("relation", "edge-arrowhead",
              [tip, (ax + px * half, ay + py * half), (ax - px * half, ay - py * half)],
              fill=FRAME, stroke=FRAME, width=0.1, panel=pid, evidence=ev,
              edge_id=edge.get("id"), edge_kind=kind,
              **{"from": edge.get("from"), "to": edge.get("to")})


def _emit_tbar(b, pid, edge, tip, dirv, ev):
    L = math.hypot(dirv[0], dirv[1]) or 1.0
    px, py = -dirv[1] / L, dirv[0] / L
    half = EDGE_HEAD_MM * 0.6
    b.line("relation", "edge-tbar", (tip[0] + px * half, tip[1] + py * half),
           (tip[0] - px * half, tip[1] - py * half), stroke=FRAME,
           width=EDGE_W_MM * 1.7, panel=pid, evidence=ev,
           edge_id=edge.get("id"), edge_kind="inhibition",
           **{"from": edge.get("from"), "to": edge.get("to")})


def _boxes_hit(box, others, pad=0.15):
    """Total overlap area of `box` with any of `others`."""
    total = 0.0
    for o in others:
        x = min(box["x"] + box["w"] + pad, o["x"] + o["w"] + pad) - max(box["x"] - pad, o["x"] - pad)
        y = min(box["y"] + box["h"] + pad, o["y"] + o["h"] + pad) - max(box["y"] - pad, o["y"] - pad)
        if x > 0 and y > 0:
            total += x * y
    return total


def _emit_edge_label(b, pid, edge, pts, area, fs, sizes, ev, node_boxes=None):
    best_i, best_len = 0, -1.0
    for i in range(len(pts) - 1):
        L = math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        if L > best_len:
            best_i, best_len = i, L
    if best_len <= 0:
        return
    mx = (pts[best_i][0] + pts[best_i + 1][0]) / 2.0
    my = (pts[best_i][1] + pts[best_i + 1][1]) / 2.0
    txt = str(edge.get("label"))
    tw = measure.text_width_mm(txt, sizes["edge_label"], fs) + 1.2
    th = measure.line_height_mm(sizes["edge_label"], 1.15)
    others = [v for k, v in (node_boxes or {}).items()]
    # slide the plate along the path until it clears every node box, then try a
    # small perpendicular offset; keep the least-overlapping candidate
    seg = (pts[best_i], pts[best_i + 1])
    dx = seg[1][0] - seg[0][0]
    dy = seg[1][1] - seg[0][1]
    L = math.hypot(dx, dy) or 1.0
    ux, uy = dx / L, dy / L
    px, py = -uy, ux
    chosen = None
    for along in (-0.35, -0.2, 0.0, 0.2, 0.35):
        for off in (0.0, 1.4, -1.4, 2.4, -2.4):
            cx2 = mx + ux * along * best_len + px * off
            cy2 = my + uy * along * best_len + py * off
            cand = _bbox(_clamp(cx2 - tw / 2.0, area["x"], area["x"] + area["w"] - tw),
                         _clamp(cy2 - th / 2.0, area["y"], area["y"] + area["h"] - th),
                         tw, th)
            score = _boxes_hit(cand, others)
            if chosen is None or score < chosen[1]:
                chosen = (cand, score)
            if score <= 1e-9:
                break
        if chosen is not None and chosen[1] <= 1e-9:
            break
    cand, score = chosen
    if score > 1e-9:
        b.warn("W_EDGE_LABEL_TIGHT",
               "edge %s label %r could not fully clear the node boxes "
               "(%.1f mm2 overlap)" % (edge.get("id"), txt, score), panel=pid)
    x, y = cand["x"], cand["y"]
    b.rect("relation", "edge-label-bg", _bbox(x, y, tw, th), fill=PAGE_BG,
           stroke="none", width=0.0, panel=pid, evidence=ev,
           edge_id=edge.get("id"), edge_kind=edge.get("kind") or "flow")
    b.text("relation", "edge-label", txt, x, y, tw, th, "edge_label", color=MUTED,
           align="center", valign="middle", panel=pid, evidence=ev,
           background=PAGE_BG, allow_truncate=False)


# --------------------------------------------------------------------------- #
# charts
# --------------------------------------------------------------------------- #
def _series_evidence(b, series):
    out = []
    for s in series:
        out.extend(b.evidence_for(s))
    return out


def _color_map(legend, n_series):
    """One colour map shared by chart series and legend swatches.

    Explicit legend colours win; otherwise both come from the same
    ``scene.categorical_colors`` call, so a series and the swatch that labels
    it can never drift apart (gate D pairs them).
    """
    items = [it for it in ((legend or {}).get("items") or []) if isinstance(it, dict)]
    fallback = scene.categorical_colors(max(len(items), n_series, 1))
    out = {}
    for i, it in enumerate(items):
        label = str(it.get("label") or "")
        out[label.strip().lower()] = it.get("color") or fallback[i % len(fallback)]
    return out


def _series_colors(chart, color_map, n_series):
    fallback = scene.categorical_colors(max(n_series, 1))
    out = []
    for i, s in enumerate(chart.get("series") or []):
        name = str((s or {}).get("name", "")).strip().lower()
        out.append(color_map.get(name) or fallback[i % len(fallback)])
    return out


DRAWABLE_CHARTS = ("bar", "barh", "line", "scatter", "dot", "heatmap", "box",
                   "violin", "waterfall")
CAT_LABEL_MAX_LINES = 3


def _with_unit(label, unit):
    label = (label or "").strip()
    unit = (unit or "").strip()
    if label and unit:
        return "%s (%s)" % (label, unit)
    return label or ("(%s)" % unit if unit else "")


def _num_or(v, default=None):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _axis_cfg(chart):
    """Normalise CONTRACT §6 ``axis.x``/``axis.y`` into two plain dicts."""
    axis = chart.get("axis") if isinstance(chart.get("axis"), dict) else {}
    out = {}
    for key in ("x", "y"):
        ax = axis.get(key) if isinstance(axis.get(key), dict) else {}
        scale = ax.get("scale")
        out[key] = {
            "label": ax.get("label") if isinstance(ax.get("label"), str) else None,
            "unit": ax.get("unit") if isinstance(ax.get("unit"), str) else None,
            "scale": scale if scale in scene.AXIS_SCALES else "linear",
            "tick_start": _num_or(ax.get("tick_start")),
            "tick_end": _num_or(ax.get("tick_end")),
            "tick_step": _num_or(ax.get("tick_step")),
        }
    return out


def _axis_text(chart, cfg, kind):
    """Resolve which axis is quantitative for this kind, plus label+unit text."""
    xl = cfg["x"].get("label") or chart.get("xlabel") \
        or (chart.get("x") or {}).get("label") or ""
    yl = cfg["y"].get("label") or chart.get("ylabel") or ""
    xu = cfg["x"].get("unit") or chart.get("x_unit") or ""
    yu = cfg["y"].get("unit") or chart.get("unit") or ""
    if kind == "barh":
        # a horizontal bar chart measures along x; chart.unit is the value unit
        q = {"label": xl, "unit": xu or chart.get("unit") or "", "cfg": cfg["x"]}
        c = {"label": yl, "unit": yu if cfg["y"].get("unit") else "", "cfg": cfg["y"]}
    else:
        q = {"label": yl, "unit": yu, "cfg": cfg["y"]}
        c = {"label": xl, "unit": xu, "cfg": cfg["x"]}
    return {"q": q, "c": c}


def _pow10_ticks(lo, hi):
    if lo is None or hi is None or lo <= 0 or hi <= 0 or hi < lo:
        return []
    k0 = int(math.floor(math.log10(lo)))
    k1 = int(math.ceil(math.log10(hi)))
    if k1 - k0 > 12:
        return []
    return [10.0 ** k for k in range(k0, k1 + 1)]


def _nice_ticks(lo, hi, count=4):
    """1/2/2.5/5 x 10^k ticks covering [lo, hi] (deterministic)."""
    if hi <= lo:
        return [lo, lo + 1.0]
    raw = (hi - lo) / float(max(2, count))
    mag = 10.0 ** math.floor(math.log10(raw))
    step = None
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= m * mag + 1e-12:
            step = m * mag
            break
    step = step or 10.0 * mag
    t0 = math.floor(lo / step + 1e-9) * step
    vals, k = [], 0
    while k < 40:
        v = t0 + k * step
        if v > hi + 1e-9:
            break
        if v >= lo - 1e-9:
            vals.append(round(v, 12))
        k += 1
    if len(vals) < 2:
        vals = [lo, lo + step]
    return vals


def _resolve_ticks(cfg, lo, hi):
    """(lo, hi, ticks) honouring scale / tick_start / tick_end / tick_step."""
    start, end, step = cfg.get("tick_start"), cfg.get("tick_end"), cfg.get("tick_step")
    if cfg.get("scale") == "log10":
        if start is not None:
            lo = max(lo, start)
        if end is not None and end > lo:
            hi = end
        if lo <= 0:
            lo = 1.0
        ticks = _pow10_ticks(lo, hi)
        if len(ticks) < 2:
            ticks = [lo, hi if hi > lo else lo * 10.0]
        return min(lo, ticks[0]), max(hi, ticks[-1]), ticks
    if start is not None and end is not None and end > start:
        lo, hi = float(start), float(end)
        if step is not None and step > 0:
            n = int(math.floor((hi - lo) / step + 1e-9))
            ticks = [round(lo + i * step, 12) for i in range(n + 1)]
            if abs(ticks[-1] - hi) > 1e-9:
                ticks.append(round(hi, 12))
        else:
            ticks = [lo + (hi - lo) * i / 4.0 for i in range(5)]
        return lo, hi, ticks
    ticks = _nice_ticks(lo, hi, 4)
    return min(lo, ticks[0]), max(hi, ticks[-1]), ticks


def _t_of(v, lo, hi, scale):
    """Fraction of v within [lo, hi] (log10 when asked), clamped to 0..1."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if scale == "log10" and lo > 0 and hi > 0 and v > 0:
        return _clamp((math.log10(v) - math.log10(lo))
                      / (math.log10(hi) - math.log10(lo)), 0.0, 1.0)
    return _clamp(0.0 if hi <= lo else (v - lo) / (hi - lo), 0.0, 1.0)


# ---- real statistics (never estimated) ------------------------------------- #
def _quantile(sorted_vals, q):
    """Linear-interpolation quantile (R type 7) of an already sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = (n - 1) * float(q)
    i = int(math.floor(pos))
    frac = pos - i
    if i + 1 >= n:
        return float(sorted_vals[-1])
    a, b = float(sorted_vals[i]), float(sorted_vals[i + 1])
    return a + (b - a) * frac


def five_number(values):
    """Tukey five-number summary + 1.5*IQR whiskers, from the real samples."""
    s = sorted(float(v) for v in values if isinstance(v, (int, float))
               and not isinstance(v, bool))
    if not s:
        return None
    q1, med, q3 = _quantile(s, 0.25), _quantile(s, 0.5), _quantile(s, 0.75)
    iqr = q3 - q1
    fence_lo, fence_hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inside = [v for v in s if fence_lo - 1e-12 <= v <= fence_hi + 1e-12]
    outliers = [v for v in s if v < fence_lo - 1e-12 or v > fence_hi + 1e-12]
    return {"n": len(s), "min": s[0], "q1": q1, "median": med, "q3": q3,
            "max": s[-1], "iqr": iqr,
            "whisker_low": min(inside) if inside else q1,
            "whisker_high": max(inside) if inside else q3,
            "outliers": outliers}


def kde_curve(values, lo, hi, steps=64):
    """Gaussian KDE on a fixed grid; Silverman bandwidth.  Real computation."""
    s = [float(v) for v in values if isinstance(v, (int, float))
         and not isinstance(v, bool)]
    n = len(s)
    if n < 2 or hi <= lo:
        return None
    mean = sum(s) / n
    var = sum((v - mean) ** 2 for v in s) / float(n - 1)
    sd = math.sqrt(var)
    if sd <= 1e-12:
        return None
    h = 1.06 * sd * (n ** (-0.2))          # Silverman's rule of thumb
    if h <= 0:
        return None
    grid = [lo + (hi - lo) * i / float(steps - 1) for i in range(steps)]
    dens, norm = [], n * h * math.sqrt(2.0 * math.pi)
    for g in grid:
        tot = 0.0
        for v in s:
            z = (g - v) / h
            tot += math.exp(-0.5 * z * z)
        dens.append(tot / norm)
    return {"grid": grid, "density": dens, "bandwidth": h, "n": n,
            "median": _quantile(sorted(s), 0.5)}


def _distribution_groups(chart, series, cats):
    """[(label, [samples], series_index)] for box/violin, from real values.

    Rule (documented so a reviewer can contest it):
      * when ``categories`` is present and EVERY category column holds >= 2
        samples across the series, each category is one distribution;
      * otherwise each series is one distribution.
    Nothing is ever estimated: a group with < 2 samples is skipped and reported
    by the caller instead of being drawn as a fake box.
    """
    if cats:
        cols = []
        for j in range(len(cats)):
            col = []
            for s in series:
                vs = s.get("values") or []
                if j < len(vs) and isinstance(vs[j], (int, float)) \
                        and not isinstance(vs[j], bool):
                    col.append(float(vs[j]))
            cols.append(col)
        if cols and all(len(c) >= 2 for c in cols):
            return [(cats[j], cols[j], None) for j in range(len(cats))], "category"
    out = []
    for si, s in enumerate(series):
        vs = [float(v) for v in (s.get("values") or [])
              if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if vs:
            name = s.get("name")
            if not (isinstance(name, str) and name.strip()) and si < len(cats):
                name = cats[si]
            out.append((str(name if name is not None else si), vs, si))
    return out, "series"


def _category_lines(cats, size_pt, fs, slot_w, max_lines=CAT_LABEL_MAX_LINES):
    """(wrapped_lines_per_category, needed_line_count, ellipsised?) per label."""
    out, need, clipped = [], 0, False
    for cat in cats:
        lines = measure.wrap_text(str(cat), size_pt, fs, max(3.0, slot_w))
        if len(lines) > max_lines:
            clipped = True
            kept = lines[:max_lines]
            last = kept[-1]
            while last and measure.text_width_mm(last + "\u2026", size_pt, fs) > slot_w:
                cut = last.rfind(" ")
                last = last[:cut] if cut > 0 else last[:-1]
            kept[-1] = last.rstrip() + "\u2026"
            lines = kept
        out.append(lines)
        need = max(need, len(lines))
    return out, max(1, need), clipped


def _mark(b, plan, group, suffix, index, value, prim=None, **extra):
    """Append one CONTRACT §4.1 mark record (stable id, evidence, value)."""
    si = extra.pop("series_index", 0)
    s = plan["series"][si] if si < len(plan["series"]) else {}
    sev = _series_evidence(b, [s]) if s else []
    src = b.src_of(sev[0]) if sev else None
    mid = "%s#%s#%s:%s" % (plan["cid"], group, suffix, index)
    rec = {
        "chart_id": plan["cid"], "panel_id": plan["pid"],
        "chart_type": plan["kind"], "kind": extra.pop("kind", suffix),
        "series": s.get("name"), "series_index": si,
        "category": extra.pop("category", None),
        "value": None if value is None else float(value),
        "index": index, "unit": plan["unit"],
        "element": (prim or {}).get("element"),
        "primitive_id": (prim or {}).get("id"),
        "evidence": list(sev),
        "source": src,
    }
    rec.update(extra)
    return b.mark(mid, **rec)


def _box_rect_y(v, lo, hi, plot, scale):
    return plot["y"] + plot["h"] * (1.0 - _t_of(v, lo, hi, scale))


def _emit_chart(b, pid, chart, box, fs, sizes, legend, colorbars):
    """Draw one chart: axes (with units), the chosen mark type, error bars,
    significance brackets and sample-size annotations.  Every drawn data mark is
    recorded in ``b.marks`` with a CONTRACT §4.1 mark_id."""
    cid = chart.get("id")
    ctype = chart.get("type") or "bar"
    kind = ctype if ctype in DRAWABLE_CHARTS else "bar"
    title = str(chart.get("title") or cid or "chart")
    unit = str(chart.get("unit") or "").strip()
    series = [s for s in (chart.get("series") or []) if isinstance(s, dict)]
    colors = _series_colors(chart, _color_map(legend, len(series)), len(series))
    ev = _series_evidence(b, series)
    cats = [str(c) for c in (chart.get("categories") or [])]
    if not cats and kind in ("bar", "barh", "waterfall", "dot"):
        nmax = max([len(s.get("values") or []) for s in series] or [0])
        cats = [fmt_num(i + 1) for i in range(nmax)]
        if cats:
            b.warn("W_CAT_IMPLICIT",
                   "chart %s has no categories[]; index labels %s are used"
                   % (cid, cats), panel=pid)
    b.add("chart", "chart", box, "chart-frame", panel=pid, evidence=ev,
          style={"fill": PAGE_BG, "stroke": GRID, "stroke_width_mm": 0.25},
          chart_id=cid, chart_type=ctype, title=title, chart_unit=unit,
          categories=list(cats),
          x={"label": (chart.get("x") or {}).get("label"),
             "values": list((chart.get("x") or {}).get("values") or [])},
          series=[{"name": s.get("name"), "values": list(s.get("values") or []),
                   "color": colors[i], "evidence": _series_evidence(b, [s]),
                   "n": scene.series_n(s), "error_kind": s.get("error_kind"),
                   "x": list(s.get("x") or []) if isinstance(s.get("x"), list) else None,
                   "errors": list(s.get("errors") or []) if isinstance(s.get("errors"), list) else None}
                  for i, s in enumerate(series)])

    pad_x = 1.0
    tw = max(4.0, box["w"] - 2 * pad_x)
    title_txt = title if not unit else "%s (%s)" % (title, unit)
    t_lines, t_h, _ = _wrap_need(title_txt, sizes["chart_title"], fs, tw,
                                 TITLE_LEADING, True)
    b.text("chart", "chart-title", title_txt, box["x"] + pad_x, box["y"] + 0.4, tw,
           t_h + 0.25, "chart_title", bold=True, color=INK, align="center",
           valign="middle", panel=pid, evidence=ev, allow_truncate=False,
           leading=TITLE_LEADING)

    kind = ctype if ctype in DRAWABLE_CHARTS else "bar"
    if ctype == "none":
        b.warn("W_CHART_EMPTY", "chart %s has type 'none' (title only)" % cid, panel=pid)
        return
    if ctype != kind:
        b.warn("W_CHART_TYPE_UNKNOWN",
               "chart %s type %r is not drawable; drawn as a bar chart"
               % (cid, ctype), panel=pid)
    horizontal = (kind == "barh")
    cfg = _axis_cfg(chart)
    txt = _axis_text(chart, cfg, kind)
    scale = txt["q"]["cfg"].get("scale") or "linear"
    force_zero = kind in scene.BAR_LIKE_TYPES
    log10 = (scale == "log10")
    if log10 and force_zero:
        b.warn("W_AXIS_LOG_BAR",
               "chart %s is %s (zero baseline) and asked for a log10 value axis; "
               "drawn linear instead" % (cid, kind), panel=pid)
        log10 = False
    lo, hi = _value_bounds(series, force_zero=force_zero, log10=log10)
    if log10 and lo <= 0:
        log10 = False
        b.warn("W_AXIS_LOG_FALLBACK",
               "chart %s asked for log10 but its data reaches %s; drawn linear"
               % (cid, layout_fmt(lo)), panel=pid)
    lo, hi, ticks = _resolve_ticks(txt["q"]["cfg"], lo, hi)
    axis_scale = "log10" if log10 else "linear"

    axis_pt = sizes["axis"]
    tick_h = measure.line_height_mm(axis_pt, 1.1)
    qlab = _with_unit(txt["q"]["label"], txt["q"]["unit"])
    clab = _with_unit(txt["c"]["label"], txt["c"]["unit"])
    above_txt = clab if horizontal else qlab
    below_txt = qlab if horizontal else clab

    # ---- sample-size annotation text (real n from the spec) ---------------- #
    n_pairs, n_common = _n_annotation(series, cats)
    n_h = tick_h if n_pairs else 0.0

    # ---- widths: gutter measured from the real tick labels ----------------- #
    tick_labels = [fmt_num(v) for v in ticks]
    widest_tick = max([measure.text_width_mm(s_, axis_pt, fs) for s_ in tick_labels] or [0.0])
    right = 1.4
    if kind in ("bar", "barh", "waterfall"):
        cat_max_w = max([measure.text_width_mm(c, axis_pt, fs) for c in cats] or [0.0])
    else:
        cat_max_w = 0.0
    if horizontal:
        slot_h = 0.0
        cat_slot_w = _clamp(max(cat_max_w, 8.0), 8.0, box["w"] * 0.34)
        cat_lines, cat_need, cat_clipped = _category_lines(cats, axis_pt, fs, cat_slot_w)
        left = cat_slot_w + 1.4
    else:
        left = widest_tick + 2.6
        cat_lines, cat_need, cat_clipped = [], 1, False
    left = _clamp(left, 4.0, box["w"] * 0.45)
    plot_w = max(6.0, box["w"] - left - right)
    if not horizontal and cats:
        slot_w = plot_w / float(len(cats))
        cat_lines, cat_need, cat_clipped = _category_lines(cats, axis_pt, fs, slot_w)
    if cat_clipped:
        b.warn("W_CAT_LABEL_TRUNCATED",
               "chart %s: %d category label(s) need more than %d lines in a %.1f mm "
               "slot; ellipsised" % (cid, len(cats), CAT_LABEL_MAX_LINES,
                                     plot_w / max(1, len(cats))), panel=pid)
    cat_h = cat_need * measure.line_height_mm(axis_pt, 1.05)
    top = box["y"] + 0.4 + t_h + (tick_h * 0.6 if above_txt else 0.0)
    above_h = tick_h if above_txt else 0.0
    if horizontal:
        bottom = tick_h * 1.6 + (tick_h if below_txt else 0.0)
    else:
        bottom = cat_h + n_h + (tick_h if below_txt else 0.0) + 0.8
    plot = _bbox(box["x"] + left, top + above_h, plot_w,
                 max(4.0, box["y"] + box["h"] - 1.0 - (top + above_h) - bottom))
    if plot["w"] < CHART_MIN_W_MM * 0.5 or plot["h"] < 12.0:
        b.warn("W_CHART_TINY",
               "chart %s has a %.1f x %.1f mm plot area" % (cid, plot["w"], plot["h"]),
               panel=pid)
    b.rect("chart", "chart-plot", plot, fill="none", stroke=GRID, width=0.25,
           panel=pid, evidence=ev, chart_id=cid, chart_type=ctype)

    plan = {"chart": chart, "kind": kind, "cid": cid, "pid": pid, "plot": plot,
            "colors": colors, "series": series, "cats": cats, "ev": ev,
            "lo": lo, "hi": hi, "ticks": ticks, "scale": axis_scale,
            "unit": unit, "left": left, "fs": fs, "sizes": sizes,
            "horizontal": horizontal, "widths": b.widths,
            "tick_h": tick_h, "cat_lines": cat_lines, "cat_need": cat_need,
            "cat_clipped": cat_clipped, "cat_h": cat_h,
            "x_scale": cfg["x"].get("scale") or "linear",
            "n_pairs": n_pairs, "n_common": n_common, "n_h": n_h,
            "above_txt": above_txt, "below_txt": below_txt,
            "sig": [g for g in (chart.get("significance") or []) if isinstance(g, dict)],
            "q_label": txt["q"]["label"], "q_unit": txt["q"]["unit"],
            "c_label": txt["c"]["label"], "c_unit": txt["c"]["unit"]}

    _draw_axes(b, plan)
    _draw_categories(b, plan)
    if kind in ("bar", "barh"):
        _draw_bars(b, plan)
    elif kind == "waterfall":
        _draw_waterfall(b, plan)
    elif kind == "line":
        _draw_line(b, plan)
    elif kind in ("scatter", "dot"):
        _draw_points(b, plan, dot=(kind == "dot"))
    elif kind == "heatmap":
        _draw_heatmap(b, plan, colorbars)
    elif kind == "box":
        _draw_boxplot(b, plan)
    elif kind == "violin":
        _draw_violin(b, plan)

    _draw_errorbars(b, plan)
    _draw_significance(b, plan)
    _draw_nlabels(b, plan)

    # ---- quantitative axis label (above-left) and category label (below) ---- #
    if above_txt:
        b.text("chart", "chart-axis-label", above_txt, plot["x"], top + 0.1,
               max(4.0, box["w"] - left - right), tick_h, "axis", color=MUTED,
               align="left", valign="top", panel=pid, evidence=ev,
               max_lines=1, allow_truncate=False)
    if below_txt:
        if horizontal:
            ly = plot["y"] + plot["h"] + tick_h * 1.4
            b.text("chart", "chart-axis-label", below_txt, plot["x"], ly,
                   plot["w"], tick_h, "axis", color=MUTED, align="center",
                   valign="top", panel=pid, evidence=ev, max_lines=1,
                   allow_truncate=False)
        else:
            ny = plot["y"] + plot["h"] + cat_h + n_h
            b.text("chart", "chart-axis-label", below_txt, plot["x"], ny,
                   plot["w"], tick_h, "axis", color=MUTED, align="center",
                   valign="top", panel=pid, evidence=ev, max_lines=1,
                   allow_truncate=False)


def layout_fmt(v):
    return fmt_num(v)


def _n_annotation(series, cats):
    """(per-category n pairs, single common n) straight from ``series[].n``."""
    per = []
    ncat = max([len(s.get("values") or []) for s in series] or [0])
    ncat = max(ncat, len(cats))
    for i in range(ncat):
        vals = []
        for s in series:
            n = scene.series_n(s, i)
            if n is not None:
                vals.append(n)
        per.append((i, min(vals) if vals else None, max(vals) if vals else None))
    pairs = [(i, mn, mx) for (i, mn, mx) in per if mn is not None]
    common = None
    if pairs:
        alln = sorted({v for (_i, mn, mx) in pairs for v in (mn, mx)})
        if len(alln) == 1:
            common = alln[0]
    return pairs, common



def _value_bounds(series, colorbars=None, force_zero=False, log10=False):
    """(lo, hi) of the quantitative axis, straight from the drawn values.

    Error bars are included (the whisker must fit), bar-like charts are forced to
    include zero (CONTRACT §7 E915), and a log10 axis only ever sees positive
    values — the caller falls back to linear when there are none.
    """
    vals = []
    for s in series:
        vs = [v for v in (s.get("values") or [])
              if isinstance(v, (int, float)) and not isinstance(v, bool)]
        vals.extend(float(v) for v in vs)
        errs = s.get("errors")
        if isinstance(errs, list):
            for v, e in zip(vs, errs):
                if isinstance(e, (int, float)) and not isinstance(e, bool):
                    vals.extend([float(v) - float(e), float(v) + float(e)])
    if not vals:
        return 0.0, 1.0
    if log10:
        pos = [v for v in vals if v > 0]
        if not pos:
            return 1.0, 10.0
        lo, hi = min(pos), max(pos)
        if hi <= lo:
            hi = lo * 10.0
        return lo, hi
    lo, hi = min(vals), max(vals)
    if force_zero:
        lo = min(lo, 0.0)
        hi = max(hi, 0.0)
    elif lo > 0.0:
        lo = 0.0
    if hi <= lo:
        hi = lo + 1.0
    if hi > 0:
        hi *= 1.05
    elif hi < 0:
        hi *= 0.95
    else:
        hi = 1.0
    return lo, hi


def _q_frac(plan, v):
    return _t_of(v, plan["lo"], plan["hi"], plan["scale"])


def _q_y(plan, v):
    plot = plan["plot"]
    return plot["y"] + plot["h"] * (1.0 - _q_frac(plan, v))


def _q_x(plan, v):
    plot = plan["plot"]
    return plot["x"] + plot["w"] * _q_frac(plan, v)


def _slots(plan):
    """[(x, width)] per category for vertical charts (slot geometry, not data)."""
    plot, cats = plan["plot"], plan["cats"]
    n = max(1, len(cats))
    w = plot["w"] / float(n)
    return [(plot["x"] + i * w, w) for i in range(len(cats))]


def _slots_h(plan):
    plot, cats = plan["plot"], plan["cats"]
    n = max(1, len(cats))
    h = plot["h"] / float(n)
    return [(plot["y"] + i * h, h) for i in range(len(cats))]


def _x_domain(plan):
    """(xlo, xhi, scale) for continuous-x charts (series[].x > chart.x.values)."""
    xs = []
    for s in plan["series"]:
        vals = scene.series_values(s)
        xs.extend(scene.series_x(plan["chart"], s, len(vals)))
    if not xs:
        return 0.0, 1.0, "linear"
    lo, hi = min(xs), max(xs)
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi, plan.get("x_scale") or "linear"


def _cont_x(plan, v, xdom):
    plot = plan["plot"]
    return plot["x"] + plot["w"] * _t_of(v, xdom[0], xdom[1], xdom[2])


def _draw_axes(b, plan):
    """Axis frame, gridlines, ticks and the zero baseline (real tick values)."""
    plot, fs, sizes = plan["plot"], plan["fs"], plan["sizes"]
    cid, pid, ev = plan["cid"], plan["pid"], plan["ev"]
    w = plan["widths"]["axis"]
    th = plan["tick_h"]
    gutter = max(3.0, plan["left"] - 2.0)
    horizontal = plan["horizontal"]
    b.line("chart", "chart-axis", (plot["x"], plot["y"]),
           (plot["x"], plot["y"] + plot["h"]), stroke=FRAME, width=w,
           panel=pid, evidence=ev, chart_id=cid)
    b.line("chart", "chart-axis", (plot["x"], plot["y"] + plot["h"]),
           (plot["x"] + plot["w"], plot["y"] + plot["h"]), stroke=FRAME,
           width=w, panel=pid, evidence=ev, chart_id=cid)
    prev_edge = None
    for v in plan["ticks"]:
        if horizontal:
            px = _q_x(plan, v)
            b.line("chart", "chart-gridline", (px, plot["y"]),
                   (px, plot["y"] + plot["h"]), stroke=GRID, width=0.2,
                   panel=pid, evidence=ev, chart_id=cid)
            b.line("chart", "chart-tick", (px, plot["y"] + plot["h"]),
                   (px, plot["y"] + plot["h"] + TICK_LEN_MM), stroke=FRAME,
                   width=0.25, panel=pid, evidence=ev, chart_id=cid)
            twid = measure.text_width_mm(fmt_num(v), plan["sizes"]["axis"], fs) / 2.0 + 0.4
            if prev_edge is not None and px - twid < prev_edge:
                continue
            prev_edge = px + twid
            b.text("chart", "chart-tick", fmt_num(v), px - twid,
                   plot["y"] + plot["h"] + 0.2, twid * 2, th, "axis",
                   color=MUTED, align="center", valign="top", panel=pid,
                   evidence=ev, max_lines=1, allow_truncate=True)
        else:
            py = _q_y(plan, v)
            b.line("chart", "chart-gridline", (plot["x"], py),
                   (plot["x"] + plot["w"], py), stroke=GRID, width=0.2,
                   panel=pid, evidence=ev, chart_id=cid)
            b.line("chart", "chart-tick", (plot["x"] - TICK_LEN_MM, py),
                   (plot["x"], py), stroke=FRAME, width=0.25, panel=pid,
                   evidence=ev, chart_id=cid)
            b.text("chart", "chart-tick", fmt_num(v),
                   max(plot["x"] - gutter, plot["x"] - 18.0), py - th / 2.0,
                   gutter - 1.4, th, "axis", color=MUTED, align="right",
                   valign="middle", panel=pid, evidence=ev, max_lines=1,
                   allow_truncate=True)
    # zero baseline: with a zero-based bar chart the frame bottom IS zero; when
    # the data straddle zero the real baseline is drawn and labelled.
    if plan["scale"] != "log10" and plan["lo"] < 0.0 < plan["hi"]:
        if horizontal:
            px = _q_x(plan, 0.0)
            b.line("chart", "chart-baseline", (px, plot["y"]),
                   (px, plot["y"] + plot["h"]), stroke=FRAME, width=w,
                   panel=pid, evidence=ev, chart_id=cid, baseline=0.0)
        else:
            py = _q_y(plan, 0.0)
            b.line("chart", "chart-baseline", (plot["x"], py),
                   (plot["x"] + plot["w"], py), stroke=FRAME, width=w,
                   panel=pid, evidence=ev, chart_id=cid, baseline=0.0)


def _draw_categories(b, plan):
    """Category labels: wrapped, never silently cropped (gate L)."""
    if not plan["cats"]:
        return
    plot, fs = plan["plot"], plan["fs"]
    cid, pid, ev = plan["cid"], plan["pid"], plan["ev"]
    th = plan["tick_h"]
    horizontal = plan["horizontal"]
    clipped = plan.get("cat_clipped")
    if horizontal:
        max_w = max(6.0, plan["left"] - 1.6)
        for i, (y, h) in enumerate(_slots_h(plan)):
            if i >= len(plan["cats"]):
                break
            b.text("chart", "chart-catlabel", plan["cats"][i],
                   plot["x"] - 1.4 - max_w, y, max_w, h, "axis", color=MUTED,
                   align="right", valign="middle", panel=pid, evidence=ev,
                   max_lines=CAT_LABEL_MAX_LINES, allow_truncate=bool(clipped))
    else:
        cat_h = plan["cat_need"] * measure.line_height_mm(plan["sizes"]["axis"], 1.05)
        for i, (x, w) in enumerate(_slots(plan)):
            if i >= len(plan["cats"]):
                break
            b.text("chart", "chart-catlabel", plan["cats"][i], x,
                   plot["y"] + plot["h"] + 0.2, w, cat_h, "axis", color=MUTED,
                   align="center", valign="top", panel=pid, evidence=ev,
                   max_lines=CAT_LABEL_MAX_LINES, allow_truncate=bool(clipped))


def _draw_bars(b, plan):
    """Grouped or stacked bars/columns, always from the shared zero baseline."""
    plot, cats = plan["plot"], plan["cats"]
    cid, pid, ev = plan["cid"], plan["pid"], plan["ev"]
    ns = max(1, len(plan["series"]))
    stacked = bool(plan["chart"].get("stacked"))
    if plan["horizontal"]:
        slots = _slots_h(plan)
        for ci, (y, h) in enumerate(slots):
            area, off = h * 0.78, h * 0.11
            bar_h = area / ns
            base = 0.0
            for si, s in enumerate(plan["series"]):
                vals = scene.series_values(s)
                if ci >= len(vals):
                    continue
                v = vals[ci]
                y0 = y + off + (0.0 if stacked else si * bar_h)
                x_base = _q_x(plan, base if stacked else 0.0)
                x_v = _q_x(plan, base + v if stacked else v)
                prim = b.rect("chart", "chart-bar",
                              _bbox(min(x_base, x_v), y0, max(0.2, abs(x_v - x_base)),
                                    bar_h * 0.9), fill=plan["colors"][si],
                              stroke="none", width=0.0, panel=pid,
                              evidence=_series_evidence(b, [s]), chart_id=cid,
                              series_name=s.get("name"), series_index=si,
                              category=cats[ci] if ci < len(cats) else None,
                              value=float(v), unit=plan["unit"],
                              error_kind=s.get("error_kind"))
                base += v
                _mark(b, plan, "series:%d" % si, "bar", ci, v, prim,
                      kind="bar", category=cats[ci] if ci < len(cats) else None,
                      series_index=si, stack_base=(base - v) if stacked else 0.0,
                      derived=bool(stacked), n=scene.series_n(s, ci))
    else:
        slots = _slots(plan)
        for ci, (x, w) in enumerate(slots):
            area, off = w * 0.78, w * 0.11
            bar_w = area / ns
            base = 0.0
            for si, s in enumerate(plan["series"]):
                vals = scene.series_values(s)
                if ci >= len(vals):
                    continue
                v = vals[ci]
                x0 = x + off + (0.0 if stacked else si * bar_w)
                y_base = _q_y(plan, base if stacked else 0.0)
                y_v = _q_y(plan, base + v if stacked else v)
                prim = b.rect("chart", "chart-bar",
                              _bbox(x0, min(y_base, y_v), bar_w * 0.9,
                                    max(0.2, abs(y_base - y_v))),
                              fill=plan["colors"][si], stroke="none", width=0.0,
                              panel=pid, evidence=_series_evidence(b, [s]),
                              chart_id=cid, series_name=s.get("name"),
                              series_index=si,
                              category=cats[ci] if ci < len(cats) else None,
                              value=float(v), unit=plan["unit"],
                              error_kind=s.get("error_kind"))
                base += v
                _mark(b, plan, "series:%d" % si, "bar", ci, v, prim,
                      kind="bar", category=cats[ci] if ci < len(cats) else None,
                      series_index=si, stack_base=(base - v) if stacked else 0.0,
                      derived=bool(stacked), n=scene.series_n(s, ci))


def _draw_waterfall(b, plan):
    """Cumulative waterfall: each bar spans [running, running+value]."""
    cats = plan["cats"]
    if not plan["series"]:
        return
    s = plan["series"][0]
    vals = scene.series_values(s)
    if not vals:
        return
    sev = _series_evidence(b, [s])
    cid, pid = plan["cid"], plan["pid"]
    cumulative = 0.0
    marks_raw = []
    if plan["horizontal"]:
        for ci, (y, h) in enumerate(_slots_h(plan)):
            if ci >= len(vals):
                break
            v = vals[ci]
            x0, x1 = _q_x(plan, cumulative), _q_x(plan, cumulative + v)
            prim = b.rect("chart", "chart-waterfall-bar",
                          _bbox(min(x0, x1), y + h * 0.11,
                                max(0.2, abs(x1 - x0)), h * 0.78),
                          fill=plan["colors"][0] if v >= 0 else "#D55E00",
                          stroke="none", width=0.0, panel=pid, evidence=sev,
                          chart_id=cid, series_name=s.get("name"),
                          category=cats[ci] if ci < len(cats) else None,
                          value=float(v), cumulative=cumulative + v)
            marks_raw.append((ci, v, cumulative, cumulative + v, prim,
                              cats[ci] if ci < len(cats) else None))
            cumulative += v
    else:
        for ci, (x, w) in enumerate(_slots(plan)):
            if ci >= len(vals):
                break
            v = vals[ci]
            y0, y1 = _q_y(plan, cumulative), _q_y(plan, cumulative + v)
            prim = b.rect("chart", "chart-waterfall-bar",
                          _bbox(x + w * 0.11, min(y0, y1), w * 0.78,
                                max(0.2, abs(y1 - y0))),
                          fill=plan["colors"][0] if v >= 0 else "#D55E00",
                          stroke="none", width=0.0, panel=pid, evidence=sev,
                          chart_id=cid, series_name=s.get("name"),
                          category=cats[ci] if ci < len(cats) else None,
                          value=float(v), cumulative=cumulative + v)
            marks_raw.append((ci, v, cumulative, cumulative + v, prim,
                              cats[ci] if ci < len(cats) else None))
            cumulative += v
    # connectors: from the top of one bar to the base of the next (real totals)
    for k in range(len(marks_raw) - 1):
        _i, _v, _b0, top, _prim, _cat = marks_raw[k]
        if plan["horizontal"]:
            slots = _slots_h(plan)
            y_top = slots[k][0] + slots[k][1] * 0.5
            y_next = slots[k + 1][0] + slots[k + 1][1] * 0.5
            x = _q_x(plan, top)
            b.line("chart", "chart-waterfall-connector", (x, y_top), (x, y_next),
                   stroke=MUTED, width=0.25, dash=[1.0, 0.8], panel=pid,
                   evidence=sev, chart_id=cid)
        else:
            slots = _slots(plan)
            x_top = slots[k][0] + slots[k][1] * 0.5
            x_next = slots[k + 1][0] + slots[k + 1][1] * 0.5
            y = _q_y(plan, top)
            b.line("chart", "chart-waterfall-connector", (x_top, y), (x_next, y),
                   stroke=MUTED, width=0.25, dash=[1.0, 0.8], panel=pid,
                   evidence=sev, chart_id=cid)
    for (ci, v, base, total, prim, cat) in marks_raw:
        _mark(b, plan, "series:0", "wf", ci, v, prim, kind="waterfall-bar",
              category=cat, series_index=0, derived=True,
              stack_base=base, cumulative=total, n=scene.series_n(s, ci))
    # total bar, when the spec asks for it
    if plan["chart"].get("total") and marks_raw:
        total = cumulative
        if plan["horizontal"]:
            slots = _slots_h(plan)
            y, h = slots[-1]
            x0, x1 = _q_x(plan, 0.0), _q_x(plan, total)
            prim = b.rect("chart", "chart-waterfall-total",
                          _bbox(min(x0, x1), y + h * 1.05, max(0.2, abs(x1 - x0)),
                                h * 0.78), fill=INK, stroke="none", width=0.0,
                          panel=pid, evidence=sev, chart_id=cid, value=float(total))
        else:
            slots = _slots(plan)
            x, w = slots[-1]
            y0, y1 = _q_y(plan, 0.0), _q_y(plan, total)
            prim = b.rect("chart", "chart-waterfall-total",
                          _bbox(x + w * 1.05, min(y0, y1), w * 0.78,
                                max(0.2, abs(y1 - y0))), fill=INK,
                          stroke="none", width=0.0, panel=pid, evidence=sev,
                          chart_id=cid, value=float(total))
        _mark(b, plan, "series:0", "wf", len(marks_raw), total, prim,
              kind="waterfall-total", category="total", series_index=0,
              derived=True, cumulative=total)


def _draw_line(b, plan):
    """Line series with real markers; error bars are added by _draw_errorbars."""
    xdom = _x_domain(plan)
    plot = plan["plot"]
    th = plan["tick_h"]
    for si, s in enumerate(plan["series"]):
        vals = scene.series_values(s)
        if not vals:
            continue
        xs = scene.series_x(plan["chart"], s, len(vals))
        sev = _series_evidence(b, [s])
        pts = [(_cont_x(plan, xs[i], xdom), _q_y(plan, vals[i]))
               for i in range(len(vals))]
        if len(pts) >= 2:
            b.path("chart", "chart-series-line", pts,
                   stroke=plan["colors"][si], width=plan["widths"]["data"],
                   panel=plan["pid"], evidence=sev, chart_id=plan["cid"],
                   series_name=s.get("name"), series_index=si,
                   evidence_ids=list(sev))
        for i, p in enumerate(pts):
            prim = b.circle("chart", "chart-series-marker", p[0], p[1], 0.7,
                            fill=plan["colors"][si], stroke=PAGE_BG, width=0.2,
                            panel=plan["pid"], evidence=sev, chart_id=plan["cid"],
                            series_name=s.get("name"), series_index=si,
                            value=float(vals[i]), unit=plan["unit"],
                            mark_id=None)
            _mark(b, plan, "series:%d" % si, "pt", i, vals[i], prim,
                  kind="point", series_index=si, x=xs[i],
                  category=plan["cats"][i] if i < len(plan["cats"]) else None,
                  n=scene.series_n(s, i))
        # x tick labels from the real x values
        prev = None
        for i, xv in enumerate(xs):
            px = _cont_x(plan, xv, xdom)
            half = max(2.5, min(6.0, plot["w"] / max(1, len(xs)) / 2.0 - 0.3))
            if prev is not None and px - half < prev:
                continue
            prev = px + half
            b.text("chart", "chart-tick", fmt_num(xv), px - half,
                   plot["y"] + plot["h"] + 0.2, half * 2, th, "axis", color=MUTED,
                   align="center", valign="top", panel=plan["pid"],
                   evidence=plan["ev"], max_lines=1, allow_truncate=True)


def _draw_points(b, plan, dot=False):
    """Scatter / dot plot: one marker per real (x, y) pair."""
    xdom = _x_domain(plan)
    for si, s in enumerate(plan["series"]):
        vals = scene.series_values(s)
        if not vals:
            continue
        xs = scene.series_x(plan["chart"], s, len(vals))
        sev = _series_evidence(b, [s])
        for i, v in enumerate(vals):
            px = _cont_x(plan, xs[i], xdom)
            py = _q_y(plan, v)
            if dot:
                prim = b.rect("chart", "chart-dot",
                              _bbox(px - 0.9, py - 0.9, 1.8, 1.8),
                              fill=plan["colors"][si], stroke="none", width=0.0,
                              panel=plan["pid"], evidence=sev,
                              chart_id=plan["cid"], series_name=s.get("name"),
                              series_index=si, value=float(v), unit=plan["unit"])
                kind = "dot"
            else:
                prim = b.circle("chart", "chart-point", px, py, 0.8,
                                fill=plan["colors"][si], stroke=PAGE_BG, width=0.2,
                                panel=plan["pid"], evidence=sev,
                                chart_id=plan["cid"], series_name=s.get("name"),
                                series_index=si, value=float(v), unit=plan["unit"])
                kind = "point"
            _mark(b, plan, "series:%d" % si, "pt", i, v, prim, kind=kind,
                  series_index=si, x=xs[i],
                  category=plan["cats"][i] if i < len(plan["cats"]) else None,
                  n=scene.series_n(s, i))


def _draw_heatmap(b, plan, colorbars):
    """Heatmap grid: one rect per real matrix cell, value printed when it fits."""
    series = plan["series"]
    vals = [v for s in series for v in scene.series_values(s)]
    if not vals:
        return
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        hi = lo + 1.0
    plot = plan["plot"]
    cmap = (colorbars[0].get("cmap") if colorbars else "viridis")
    b.add("chart", "heatmap", plot, "chart-heatmap", panel=plan["pid"],
          evidence=plan["ev"],
          style={"fill": "none", "stroke": "none", "stroke_width_mm": 0.0},
          chart_id=plan["cid"], vmin=_r(lo), vmax=_r(hi), cmap=cmap,
          series=[s.get("name") for s in series])
    per = max([len(scene.series_values(s)) for s in series] or [1])
    cw, ch = plot["w"] / float(per), plot["h"] / float(max(1, len(series)))
    for si, s in enumerate(series):
        sev = _series_evidence(b, [s])
        svals = scene.series_values(s)
        for i, v in enumerate(svals):
            prim = b.rect("chart", "heatmap-cell",
                          _bbox(plot["x"] + i * cw, plot["y"] + si * ch, cw, ch),
                          fill=cmap_color(cmap, (float(v) - lo) / (hi - lo)),
                          stroke=PAGE_BG, width=0.15, panel=plan["pid"],
                          evidence=sev, chart_id=plan["cid"],
                          series_name=s.get("name"), series_index=si,
                          value=float(v), unit=plan["unit"])
            _mark(b, plan, "series:%d" % si, "cell", "%d,%d" % (si, i), v, prim,
                  kind="heatmap-cell", series_index=si, row=si, col=i,
                  category=plan["cats"][i] if i < len(plan["cats"]) else None)
            if cw >= 5.0 and ch >= 3.4:
                b.text("chart", "heatmap-value", _cell_label(v), 
                       plot["x"] + i * cw, plot["y"] + si * ch, cw, ch, "axis",
                       color=ink_on(cmap_color(cmap, (float(v) - lo) / (hi - lo))),
                       align="center", valign="middle", panel=plan["pid"],
                       evidence=sev, max_lines=1, allow_truncate=True)
    # row labels (series names) and column labels (categories / numeric x)
    th = plan["tick_h"]
    for si, s in enumerate(series):
        name = str(s.get("name") or "")
        if not name:
            continue
        b.text("chart", "chart-rowlabel", name, plot["x"] - 1.4 - max(6.0, plan["left"] - 2.6),
               plot["y"] + si * ch, max(6.0, plan["left"] - 2.6), ch, "axis",
               color=MUTED, align="right", valign="middle", panel=plan["pid"],
               evidence=plan["ev"], max_lines=2, allow_truncate=False)
    cols = plan["cats"] or [fmt_num(v) for v in scene.chart_x(plan["chart"])]
    if len(cols) == per:
        for i in range(per):
            b.text("chart", "chart-catlabel", str(cols[i]), plot["x"] + i * cw,
                   plot["y"] + plot["h"] + 0.2, cw, th, "axis", color=MUTED,
                   align="center", valign="top", panel=plan["pid"],
                   evidence=plan["ev"], max_lines=1, allow_truncate=True)


def _cell_label(v):
    return fmt_num(v)


def _draw_boxplot(b, plan):
    """Box plot: Tukey five-number summary computed from the real samples."""
    groups, _src = _distribution_groups(plan["chart"], plan["series"], plan["cats"])
    if not groups:
        b.warn("W_BOX_NO_SAMPLES",
               "chart %s has no group with >= 1 sample" % plan["cid"], panel=plan["pid"])
        return
    plot = plan["plot"]
    n = max(1, len(plan["cats"]) if len(plan["cats"]) >= len(groups) else len(groups))
    w = plot["w"] / float(n)
    for gi, (label, samples, si) in enumerate(groups):
        s = plan["series"][si] if si is not None and si < len(plan["series"]) else \
            (plan["series"][0] if plan["series"] else {})
        sev = _series_evidence(b, [s])
        color = plan["colors"][si if si is not None else 0]
        cx = plot["x"] + (gi + 0.5) * w
        bw = w * 0.5
        st = five_number(samples)
        if st is None or st["n"] < 2:
            b.warn("W_BOX_TOO_FEW",
                   "chart %s group %r has %d sample(s); a box needs >= 2"
                   % (plan["cid"], label, 0 if st is None else st["n"]), panel=plan["pid"])
            continue
        y_q1, y_q3 = _q_y(plan, st["q1"]), _q_y(plan, st["q3"])
        y_med = _q_y(plan, st["median"])
        y_wl, y_wh = _q_y(plan, st["whisker_low"]), _q_y(plan, st["whisker_high"])
        common = dict(panel=plan["pid"], evidence=sev, chart_id=plan["cid"],
                      series_name=s.get("name") if isinstance(s, dict) else label,
                      series_index=si if si is not None else gi,
                      category=label, group=label)
        prim = b.rect("chart", "chart-box",
                      _bbox(cx - bw / 2.0, min(y_q1, y_q3), bw,
                            max(0.4, abs(y_q3 - y_q1))),
                      fill=color, stroke=FRAME, width=plan["widths"]["axis"],
                      **common)
        _mark(b, plan, "series:%d" % (si if si is not None else gi), "box", gi,
              st["median"], prim, kind="box", derived=True, group=label,
              series_index=si if si is not None else gi,
              q1=st["q1"], q3=st["q3"], median=st["median"], iqr=st["iqr"],
              n=st["n"], source_values=list(samples))
        med = _bbox(cx - bw / 2.0, y_med - 0.15, bw, 0.3)
        prim2 = b.rect("chart", "chart-box-median", med, fill=INK, stroke="none",
                       width=0.0, value=st["median"], **common)
        _mark(b, plan, "series:%d" % (si if si is not None else gi), "boxmed", gi,
              st["median"], prim2, kind="box-median", derived=True, group=label,
              series_index=si if si is not None else gi, n=st["n"],
              source_values=list(samples))
        for (ya, yb, val, tag) in ((y_wh, min(y_q1, y_q3), st["whisker_high"], "high"),
                                   (max(y_q1, y_q3), y_wl, st["whisker_low"], "low")):
            b.line("chart", "chart-box-whisker", (cx, ya), (cx, yb), stroke=FRAME,
                   width=plan["widths"]["axis"], **common)
        for ycap, val, tag in ((y_wh, st["whisker_high"], "high"),
                               (y_wl, st["whisker_low"], "low")):
            b.line("chart", "chart-box-cap", (cx - bw * 0.3, ycap),
                   (cx + bw * 0.3, ycap), stroke=FRAME,
                   width=plan["widths"]["axis"], **common)
            _mark(b, plan, "series:%d" % (si if si is not None else gi), "boxwh",
                  "%d%s" % (gi, tag), val, None, kind="box-whisker-%s" % tag,
                  derived=True, group=label,
                  series_index=si if si is not None else gi, n=st["n"],
                  source_values=list(samples))
        for oi, ov in enumerate(st["outliers"]):
            oprim = b.circle("chart", "chart-box-outlier", cx, _q_y(plan, ov), 0.55,
                             fill=PAGE_BG, stroke=color, width=plan["widths"]["axis"],
                             value=float(ov), **common)
            _mark(b, plan, "series:%d" % (si if si is not None else gi), "boxout",
                  "%d,%d" % (gi, oi), ov, oprim, kind="box-outlier", derived=True,
                  group=label, series_index=si if si is not None else gi,
                  source_values=list(samples))


def _draw_violin(b, plan):
    """Violin: Gaussian KDE computed from the real samples (Silverman bw)."""
    groups, _src = _distribution_groups(plan["chart"], plan["series"], plan["cats"])
    if not groups:
        return
    plot = plan["plot"]
    n = max(1, len(plan["cats"]) if len(plan["cats"]) >= len(groups) else len(groups))
    w = plot["w"] / float(n)
    for gi, (label, samples, si) in enumerate(groups):
        s = plan["series"][si] if si is not None and si < len(plan["series"]) else \
            (plan["series"][0] if plan["series"] else {})
        sev = _series_evidence(b, [s])
        color = plan["colors"][si if si is not None else 0]
        gvals = sorted(float(v) for v in samples
                       if isinstance(v, (int, float)) and not isinstance(v, bool))
        if len(gvals) < 2:
            b.warn("W_VIOLIN_TOO_FEW",
                   "chart %s group %r has %d sample(s); a KDE needs >= 2"
                   % (plan["cid"], label, len(gvals)), panel=plan["pid"])
            continue
        lo_g, hi_g = gvals[0], gvals[-1]
        if hi_g <= lo_g:
            hi_g = lo_g + 1.0
        kde = kde_curve(gvals, lo_g, hi_g, steps=64)
        if not kde:
            b.warn("W_VIOLIN_KDE",
                   "chart %s group %r has zero variance; KDE skipped"
                   % (plan["cid"], label), panel=plan["pid"])
            continue
        cx = plot["x"] + (gi + 0.5) * w
        half = w * 0.34
        dmax = max(kde["density"]) or 1.0
        pts = []
        for g, d in zip(kde["grid"], kde["density"]):
            pts.append((cx - half * (d / dmax), _q_y(plan, g)))
        for g, d in reversed(list(zip(kde["grid"], kde["density"]))):
            pts.append((cx + half * (d / dmax), _q_y(plan, g)))
        common = dict(panel=plan["pid"], evidence=sev, chart_id=plan["cid"],
                      series_name=s.get("name") if isinstance(s, dict) else label,
                      series_index=si if si is not None else gi,
                      category=label, group=label)
        prim = b.polygon("chart", "chart-violin", pts,
                        fill=_tint(color, 0.55), stroke=color,
                        width=plan["widths"]["data"], closed=True, **common)
        _mark(b, plan, "series:%d" % (si if si is not None else gi), "vio", gi,
              kde["median"], prim, kind="violin", derived=True, group=label,
              bandwidth=kde["bandwidth"], n=kde["n"],
              source_values=list(gvals))
        y_med = _q_y(plan, kde["median"])
        b.line("chart", "chart-violin-median", (cx - half * 0.7, y_med),
               (cx + half * 0.7, y_med), stroke=INK,
               width=plan["widths"]["axis"], **common)


def _tint(color, amount):
    r, g, bl = _hex_rgb(color)
    mix = lambda c: int(round(c + (255 - c) * _clamp(amount, 0.0, 1.0)))
    return _rgb_hex([mix(r), mix(g), mix(bl)])


def _draw_errorbars(b, plan):
    """Error bars: cap + line, length mapped through the real axis scale."""
    horizontal = plan["horizontal"]
    xdom = None if horizontal else _x_domain(plan)
    for si, s in enumerate(plan["series"]):
        vals = scene.series_values(s)
        errs = scene.series_errors(s)
        if not errs or len(errs) != len(vals):
            continue
        kind = s.get("error_kind")
        if kind not in scene.ERROR_KINDS:
            b.warn("W_ERRBAR_UNDECLARED",
                   "chart %s series %r has error bars but no declared error_kind "
                   "(E911); they are drawn and gate K must reject the figure"
                   % (plan["cid"], s.get("name")), panel=plan["pid"])
        sev = _series_evidence(b, [s])
        xs = scene.series_x(plan["chart"], s, len(vals))
        for i, v in enumerate(vals):
            e = errs[i]
            if e is None:
                continue
            if horizontal:
                slot_y = None
                cy = _point_y(plan, si, i, len(plan["series"]))
                x0 = _q_x(plan, v - e)
                x1 = _q_x(plan, v + e)
                b.line("chart", "chart-errorbar", (x0, cy), (x1, cy),
                       stroke=plan["colors"][si], width=plan["widths"]["error"],
                       panel=plan["pid"], evidence=sev, chart_id=plan["cid"],
                       series_name=s.get("name"), category=plan["cats"][i] if i < len(plan["cats"]) else None,
                       error_kind=kind, value=float(e))
                for xc in (x0, x1):
                    b.line("chart", "chart-errorbar-cap", (xc, cy - 0.7),
                           (xc, cy + 0.7), stroke=plan["colors"][si],
                           width=plan["widths"]["error"], panel=plan["pid"],
                           evidence=sev, chart_id=plan["cid"])
                prim = None
            else:
                if plan["kind"] in ("bar", "barh", "waterfall"):
                    cx = _point_x(plan, si, i, len(plan["series"]))
                else:
                    cx = _cont_x(plan, xs[i], xdom)
                y0 = _q_y(plan, v - e)
                y1 = _q_y(plan, v + e)
                prim = b.line("chart", "chart-errorbar", (cx, y0), (cx, y1),
                              stroke=plan["colors"][si],
                              width=plan["widths"]["error"], panel=plan["pid"],
                              evidence=sev, chart_id=plan["cid"],
                              series_name=s.get("name"),
                              category=plan["cats"][i] if i < len(plan["cats"]) else None,
                              error_kind=kind, value=float(e))
                for yc in (y0, y1):
                    b.line("chart", "chart-errorbar-cap", (cx - 0.7, yc),
                           (cx + 0.7, yc), stroke=plan["colors"][si],
                           width=plan["widths"]["error"], panel=plan["pid"],
                           evidence=sev, chart_id=plan["cid"])
            _mark(b, plan, "series:%d" % si, "eb", i, e, prim,
                  kind="errorbar", series_index=si, value_center=float(v),
                  error_kind=kind, x=xs[i],
                  category=plan["cats"][i] if i < len(plan["cats"]) else None,
                  n=scene.series_n(s, i))


def _point_x(plan, si, i, nser):
    """Centre x of bar/task i of series si (vertical charts)."""
    slots = _slots(plan)
    if i >= len(slots):
        return plan["plot"]["x"]
    x, w = slots[i]
    area, off = w * 0.78, w * 0.11
    bar_w = area / max(1, nser)
    if plan["chart"].get("stacked"):
        return x + off + area / 2.0
    return x + off + si * bar_w + bar_w * 0.45


def _point_y(plan, si, i, nser):
    """Centre y of bar i of series si (horizontal charts)."""
    slots = _slots_h(plan)
    if i >= len(slots):
        return plan["plot"]["y"]
    y, h = slots[i]
    area, off = h * 0.78, h * 0.11
    bar_h = area / max(1, nser)
    if plan["chart"].get("stacked"):
        return y + off + area / 2.0
    return y + off + si * bar_h + bar_h * 0.45


def _draw_significance(b, plan):
    """Significance brackets between two named categories (label from the spec)."""
    if not plan["sig"] or not plan["cats"]:
        return
    plot = plan["plot"]
    cid, pid, ev = plan["cid"], plan["pid"], plan["ev"]
    slots = _slots(plan) if not plan["horizontal"] else _slots_h(plan)
    idx = {c: i for i, c in enumerate(plan["cats"])}
    th = plan["tick_h"]
    levels = []
    for gi, g in enumerate(plan["sig"]):
        between = g.get("between") or []
        if len(between) != 2 or between[0] not in idx or between[1] not in idx:
            b.warn("W_SIG_UNPLACED",
                   "chart %s significance %r does not name two drawn categories %s"
                   % (cid, between, plan["cats"]), panel=pid)
            continue
        i0, i1 = sorted((idx[between[0]], idx[between[1]]))
        if plan["horizontal"]:
            c0 = slots[i0][0] + slots[i0][1] * 0.5
            c1 = slots[i1][0] + slots[i1][1] * 0.5
        else:
            c0 = slots[i0][0] + slots[i0][1] * 0.5
            c1 = slots[i1][0] + slots[i1][1] * 0.5
        # vertical clearance: one level per overlapping bracket, top-down
        level = 0
        for (lo_, hi_, lv) in levels:
            if not (c1 < lo_ - 0.5 or c0 > hi_ + 0.5):
                level = max(level, lv + 1)
        levels.append((c0, c1, level))
        drop = 1.6
        if plan["horizontal"]:
            x = max(c1, c0 + 2.0) + 1.2 + level * 2.4
            b.path("chart", "chart-sig-bracket",
                   [(c0, x), (c0, x + 0.8), (c1, x + 0.8), (c1, x)],
                   stroke=INK, width=plan["widths"]["axis"], panel=pid,
                   evidence=ev, chart_id=cid, group=gi)
            b.text("chart", "chart-sig-label", str(g.get("label") or ""),
                   x + 0.4, c0 - 1.6, 3.4, min(3.2, abs(c1 - c0)), "axis",
                   color=INK, align="center", valign="middle", panel=pid,
                   evidence=ev, max_lines=1, allow_truncate=False)
        else:
            y_top = plot["y"] + 0.8 + level * 3.2
            b.path("chart", "chart-sig-bracket",
                   [(c0, y_top + drop), (c0, y_top), (c1, y_top), (c1, y_top + drop)],
                   stroke=INK, width=plan["widths"]["axis"], panel=pid,
                   evidence=ev, chart_id=cid, group=gi)
            b.text("chart", "chart-sig-label", str(g.get("label") or ""),
                   min(c0, c1), y_top - th - 0.2, max(4.0, abs(c1 - c0)), th, "axis",
                   color=INK, align="center", valign="bottom", panel=pid,
                   evidence=ev, max_lines=1, allow_truncate=False)
        _mark(b, plan, "chart", "sig", gi, g.get("p") if isinstance(g.get("p"), (int, float)) else None,
              None, kind="significance", series_index=0,
              between=[between[0], between[1]], label=g.get("label"),
              test=g.get("test"), p=g.get("p"), derived=False,
              evidence=list(ev) or None, source=b.src_of(ev[0]) if ev else None)


def _draw_nlabels(b, plan):
    """Sample-size annotations from the spec's real ``n`` values."""
    pairs, common = plan["n_pairs"], plan["n_common"]
    if not pairs:
        return
    plot = plan["plot"]
    cid, pid, ev = plan["cid"], plan["pid"], plan["ev"]
    th = plan["n_h"] or plan["tick_h"]
    cats = plan["cats"]
    per_slot = cats and plan["kind"] in ("bar", "barh", "waterfall", "dot",
                                        "box", "violin")
    if per_slot:
        if plan["horizontal"]:
            slots = _slots_h(plan)
            for (i, mn, mx) in pairs:
                if i >= len(slots):
                    continue
                y, h = slots[i]
                b.text("chart", "chart-nlabel", _n_text(mn, mx),
                       plot["x"] - 1.4 - max(6.0, plan["left"] - 1.6), y, 
                       max(6.0, plan["left"] - 1.6), h, "axis", color=MUTED,
                       align="right", valign="bottom", panel=pid, evidence=ev,
                       max_lines=1, allow_truncate=False)
        else:
            slots = _slots(plan)
            cat_h = plan["cat_need"] * measure.line_height_mm(plan["sizes"]["axis"], 1.05)
            for (i, mn, mx) in pairs:
                if i >= len(slots):
                    continue
                x, w = slots[i]
                b.text("chart", "chart-nlabel", _n_text(mn, mx), x,
                       plot["y"] + plot["h"] + cat_h, w, th, "axis", color=MUTED,
                       align="center", valign="top", panel=pid, evidence=ev,
                       max_lines=1, allow_truncate=False)
    else:
        txt = _n_text(common, common) if common is not None else \
            "/".join(sorted({_n_text(mn, mx) for (_i, mn, mx) in pairs}))
        if txt:
            b.text("chart", "chart-nlabel", txt, plot["x"] + 0.6, plot["y"] + 0.4,
                   max(6.0, plot["w"] - 1.2), th, "axis", color=MUTED,
                   align="left", valign="top", panel=pid, evidence=ev,
                   max_lines=1, allow_truncate=True)


def _n_text(mn, mx):
    if mn is None:
        return ""
    if mx is None or mn == mx:
        return "n=%s" % fmt_num(mn)
    return "n=%s-%s" % (fmt_num(mn), fmt_num(mx))



def _legacy_removed_axis_helpers():
    """Placeholder kept so no caller can silently bind to the old axis code."""
    raise AssertionError("removed in chart-engine v1.2")



def _legacy_removed_chart_helpers():
    """Placeholder kept so no caller can silently bind to the old chart code."""
    raise AssertionError("removed in chart-engine v1.2")



def _legacy_removed_chart_helpers2():
    """Placeholder kept so no caller can silently bind to the old chart code."""
    raise AssertionError("removed in chart-engine v1.2")



# --------------------------------------------------------------------------- #
# legend / colorbar / scalebar
# --------------------------------------------------------------------------- #
def _emit_legend(b, pid, legend, box, fs, sizes, ncol=1):
    items = [it for it in (legend.get("items") or []) if isinstance(it, dict)]
    b.add("legend", "legend", box, "legend-frame", panel=pid,
          style={"fill": PAGE_BG, "stroke": GRID, "stroke_width_mm": 0.25},
          title=legend.get("title"),
          items=[{"label": it.get("label"), "color": it.get("color")} for it in items])
    row = _legend_row(sizes, fs)
    ink = INK
    y = box["y"] + 0.5
    if legend.get("title"):
        b.text("legend", "legend-title", str(legend["title"]), box["x"] + 1.0, y,
               max(2.0, box["w"] - 2.0), row, "legend", bold=True, color=ink,
               valign="middle", panel=pid, max_lines=1, allow_truncate=False)
        y += row
    ncol = max(1, min(int(ncol or 1), max(1, len(items))))
    col_w = (box["w"] - 1.4) / ncol
    sw = 2.4
    cmap_colors = _color_map(legend, len(items))
    i_col = 0
    for it in items:
        rows_used = int(math.ceil((i_col + 1) / float(ncol)))
        if y + rows_used * row > box["y"] + box["h"] + 0.3:
            b.warn("W_LEGEND_OVERFLOW",
                   "legend of panel %s does not fit its box (%d items)"
                   % (pid, len(items)), panel=pid)
            break
        color = it.get("color") or cmap_colors.get(
            str(it.get("label") or "").strip().lower()) or scene.categorical_colors(1)[0]
        ev = b.evidence_for(it)
        cx = box["x"] + 1.0 + (i_col % ncol) * col_w
        cy = y + (i_col // ncol) * row
        b.rect("legend", "legend-swatch", _bbox(cx, cy + 0.6, sw, sw * 0.7),
               fill=color, stroke=FRAME, width=0.2, panel=pid, evidence=ev)
        b.text("legend", "legend-item", str(it.get("label") or ""),
               cx + sw + 0.7, cy, max(2.0, col_w - sw - 1.4), row, "legend",
               color=ink, valign="middle", panel=pid, evidence=ev, max_lines=1,
               allow_truncate=True)
        i_col += 1


def _emit_colorbar(b, pid, cb, box, fs, sizes):
    """Horizontal colour bar: the title gets the box width, so it cannot clip."""
    cmap = cb.get("cmap")
    vmin, vmax = float(cb.get("vmin", 0.0)), float(cb.get("vmax", 1.0))
    title = str(cb.get("title") or cb.get("id") or "")
    unit = str(cb.get("unit") or "")
    ev = b.evidence_for(cb)
    ticks = [vmin + (vmax - vmin) * i / 4.0 for i in range(5)]
    oz = sizes["colorbar"]
    th = measure.line_height_mm(oz, 1.1)
    tick_h = max(3.0, th)
    # title (up to two lines, never clipped), then bar, then ticks
    title_txt = title if not unit else "%s (%s)" % (title, unit)
    t_lines, t_h, _ = _wrap_need(title_txt, oz, fs, max(4.0, box["w"] - 1.4),
                                 TITLE_LEADING, True)
    t_lines = t_lines[:2]
    t_h = min(t_h, max(2.0, box["h"] - tick_h - 4.4))
    pad = 1.0
    bar_h = _clamp(box["h"] - t_h - tick_h - 1.2, 2.6, 4.4)
    avail_w = max(8.0, box["w"] - 2 * pad)
    spacing = avail_w / float(max(1, len(ticks) - 1))
    half = max(3.0, min(6.5, spacing / 2.0 - 0.4))
    bar_w = max(4.0, avail_w - 2 * half)
    spacing = bar_w / float(max(1, len(ticks) - 1))
    half = max(3.0, min(6.5, spacing / 2.0 - 0.4))
    bar = _bbox(box["x"] + pad + half, box["y"] + t_h + 0.3, bar_w, bar_h)
    b.add("colorbar", "colorbar", box, "colorbar-frame", panel=pid, evidence=ev,
          style={"fill": "none", "stroke": "none", "stroke_width_mm": 0.0},
          colorbar_id=cb.get("id"), cmap=cmap, cmap_stops=list(scene.CMAPS.get(cmap, [])),
          vmin=_r(vmin), vmax=_r(vmax), unit=unit, title=title, orientation="horizontal",
          ticks=[_r(t) for t in ticks], bar_bbox=bar)
    b.text("colorbar", "colorbar-title", title_txt, box["x"] + 0.7, box["y"],
           max(4.0, box["w"] - 1.4), max(2.0, t_h), "colorbar", bold=True, color=INK,
           align="left", valign="top", panel=pid, evidence=ev, max_lines=2,
           allow_truncate=True, leading=TITLE_LEADING)
    steps = 40
    step_w = bar["w"] / steps
    for i in range(steps):
        t = (i + 0.5) / steps
        b.rect("colorbar", "colorbar-band",
               _bbox(bar["x"] + i * step_w, bar["y"], step_w + 0.03, bar["h"]),
               fill=cmap_color(cmap, t), stroke="none", width=0.0, panel=pid,
               evidence=ev, colorbar_id=cb.get("id"))
    b.rect("colorbar", "colorbar-outline", bar, fill="none", stroke=FRAME,
           width=0.25, panel=pid, evidence=ev)
    for t in ticks:
        frac = (t - vmin) / (vmax - vmin) if vmax > vmin else 0.0
        x = bar["x"] + bar["w"] * _clamp(frac, 0.0, 1.0)
        b.line("colorbar", "colorbar-tick", (x, bar["y"] + bar["h"]),
               (x, bar["y"] + bar["h"] + 0.9), stroke=FRAME, width=0.25,
               panel=pid, evidence=ev)
        b.text("colorbar", "colorbar-ticklabel", fmt_num(t), x - half,
               bar["y"] + bar["h"] + 1.0, half * 2, tick_h, "colorbar", color=MUTED,
               align="center", valign="top", panel=pid, evidence=ev,
               max_lines=1, allow_truncate=True)


def _maybe_scalebar(b, spec, panel_records):
    meta = spec.get("meta") or {}
    sb = meta.get("scalebar")
    if not isinstance(sb, dict):
        for p in (spec.get("panels") or []):
            if isinstance(p, dict) and isinstance(p.get("scalebar"), dict):
                sb = dict(p["scalebar"])
                sb.setdefault("panel", p.get("id"))
                break
    if not isinstance(sb, dict) or not panel_records:
        return
    target = None
    for rec in panel_records:
        if sb.get("panel") and rec["id"] == sb.get("panel"):
            target = rec
            break
    target = target or panel_records[-1]
    length = _clamp(float(sb.get("length_mm", 10.0) or 10.0), 2.0, 60.0)
    label = str(sb.get("label") or ("%s mm" % fmt_num(length)))
    box = target["content_bbox"]
    y = box["y"] + box["h"] - 3.4
    x1 = box["x"] + box["w"] - 2.0
    x0 = x1 - length
    if x0 < box["x"] + 2.0:
        x0 = box["x"] + 2.0
        x1 = x0 + length
    pid = target["id"]
    b.add("annotation", "scalebar", _bbox(x0, y - 1.0, x1 - x0, 4.4), "scalebar",
          panel=pid, style={"stroke": INK, "stroke_width_mm": 0.4},
          length_mm=_r(length), label=label)
    b.line("annotation", "scalebar-line", (x0, y), (x1, y), stroke=INK, width=0.4,
           panel=pid)
    b.line("annotation", "scalebar-tick", (x0, y - 1.0), (x0, y + 1.0), stroke=INK,
           width=0.4, panel=pid)
    b.line("annotation", "scalebar-tick", (x1, y - 1.0), (x1, y + 1.0), stroke=INK,
           width=0.4, panel=pid)
    b.text("annotation", "scalebar-label", label, x0, y - 3.8, x1 - x0, 2.8,
           "axis", color=INK, align="center", valign="bottom", panel=pid,
           max_lines=1, allow_truncate=False)

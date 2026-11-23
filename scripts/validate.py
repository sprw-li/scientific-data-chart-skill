#!/usr/bin/env python3
"""validate.py — verification gates A–L (contract §6/§7, frozen).

Owner: validate-export.  ``run_checks`` returns

    {"checks": [{"id","name","status","detail"}], "failed": [ids], "warned": [ids]}

Every check is *measured*, and every detail string carries at least one number.
Status semantics:
    pass  — the measured quantity met the declared threshold
    warn  — the property could not be established from the available inputs
            (UNVERIFIABLE) or is a documented soft-limit breach; NOT a pass
    fail  — the measured quantity breached a hard threshold

No check may return "pass" for an unmeasured property.  Nothing here weakens a
threshold to accommodate a specific artefact.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import importlib
import itertools
import json
import math
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import scene  # noqa: E402

# --------------------------------------------------------------------------- #
# thresholds (documented, calibrated in scratch/calib_c.py)
# --------------------------------------------------------------------------- #
CVD_MIN_DE = 6.0          # CIELAB ΔE76 floor for colour-coded alternatives
GRAY_MIN_DL = 5.0         # L* separation floor under grayscale conversion
WCAG_NORMAL = 4.5         # WCAG 2.1 AA body text
WCAG_LARGE = 3.0          # WCAG 2.1 AA large text (>=18pt, or >=14pt bold)
LARGE_PT = 18.0
LARGE_BOLD_PT = 14.0
BBOX_TOL_MM = 1e-6
COLORBAR_SLACK_FRAC = 0.15   # allowed axis rounding beyond the data range

# --- colour-vision-deficiency simulation (Viénot/Brettel-style, linear sRGB) --
CVD_MATRICES = {
    "protan": ((0.11238, 0.88762, 0.0), (0.11238, 0.88762, 0.0), (0.00401, 0.0, 0.99599)),
    "deutan": ((0.29275, 0.70725, 0.0), (0.29275, 0.70725, 0.0), (-0.02234, 0.0, 1.02234)),
    "tritan": ((1.0, 0.0, 0.0), (0.0, 0.12740, 0.87260), (0.0, 0.12740, 0.87260)),
}

# Helvetica advance widths /1000 (fallback only: preferred path is measure.py)
_AFM = {}
for _c, _w in zip(" !\"#$%&'()*+,-./", [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278]):
    _AFM[_c] = _w
for _i, _c in enumerate("0123456789"):
    _AFM[_c] = 556
for _c, _w in zip(":;<=>?@", [278, 278, 584, 584, 584, 556, 1015]):
    _AFM[_c] = _w
for _c, _w in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", [667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611]):
    _AFM[_c] = _w
for _c, _w in zip("[\\]^_`", [278, 278, 278, 469, 556, 333]):
    _AFM[_c] = _w
for _c, _w in zip("abcdefghijklmnopqrstuvwxyz", [556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500]):
    _AFM[_c] = _w
for _c, _w in zip("{|}~", [334, 260, 334, 584]):
    _AFM[_c] = _w

_NAMED = {"black": (0, 0, 0), "white": (1, 1, 1), "red": (1, 0, 0), "green": (0, 128 / 255, 0),
          "blue": (0, 0, 1), "gray": (128 / 255, 128 / 255, 128 / 255), "grey": (128 / 255,) * 3,
          "none": None, "transparent": None}

_FS_CACHE = {}


# --------------------------------------------------------------------------- #
# colour math
# --------------------------------------------------------------------------- #
def hex_to_rgb(value):
    """'#rgb' | '#rrggbb' | 'rgb(r,g,b)' | a few CSS names -> (r,g,b) in 0..1."""
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if s in _NAMED:
        return _NAMED[s]
    m = re.match(r"^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", s)
    if m:
        return tuple(max(0.0, min(1.0, float(m.group(i)) / 255.0)) for i in (1, 2, 3))
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) >= 6 and re.fullmatch(r"[0-9a-f]{6}", h[:6]):
            return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return None


def _linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _unlinear(c):
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def relative_luminance(rgb):
    r, g, b = (_linear(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(rgb1, rgb2):
    l1, l2 = relative_luminance(rgb1), relative_luminance(rgb2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def simulate(rgb, kind):
    """Apply a protan/deutan/tritan matrix in linear sRGB, return sRGB tuple."""
    m = CVD_MATRICES[kind]
    v = [_linear(x) for x in rgb]
    return tuple(_unlinear(sum(m[i][j] * v[j] for j in range(3))) for i in range(3))


def to_lab(rgb):
    r, g, b = (_linear(x) for x in rgb)
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    xn, yn, zn = 0.95047, 1.0, 1.08883

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29
    fx, fy, fz = f(x / xn), f(y / yn), f(z / zn)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(lab1, lab2):
    """CIE76 colour difference (Euclidean in CIELAB)."""
    return math.sqrt(sum((lab1[i] - lab2[i]) ** 2 for i in range(3)))


# --------------------------------------------------------------------------- #
# soft imports / measurement helpers
# --------------------------------------------------------------------------- #
def _soft_import(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def resolve_fs(spec):
    lang = ((spec or {}).get("meta") or {}).get("language", "auto")
    if lang in _FS_CACHE:
        return _FS_CACHE[lang]
    fs = None
    mod = _soft_import("fonts")
    if mod is not None:
        try:
            fs = mod.resolve_fonts(lang)
        except Exception:
            fs = None
    _FS_CACHE[lang] = fs
    return fs


def measure_width_mm(text, size_pt, fs, bold=False):
    """Text width in mm.  Preferred: measure.text_width_mm (render-core).

    Returns (width_mm, source) where source is 'measure' or 'afm-fallback'.
    """
    mod = _soft_import("measure")
    if mod is not None and hasattr(mod, "text_width_mm"):
        try:
            return float(mod.text_width_mm(text, size_pt, fs, bold)), "measure"
        except Exception:
            pass
    total = 0.0
    for ch in str(text):
        w = _AFM.get(ch)
        if w is None:
            w = 1000 if ord(ch) > 0x2E7F else 500   # CJK full width / default
        total += w
    if bold:
        total *= 1.05
    return total / 1000.0 * size_pt * scene.PT_MM, "afm-fallback"


def glyph_coverage(text, fs):
    """(missing_glyphs_list, source) — [] means covered."""
    mod = _soft_import("fonts")
    if mod is None or fs is None:
        return None, "unavailable"
    try:
        if hasattr(mod, "glyph_coverage_ok"):
            ok = mod.glyph_coverage_ok(text, fs)
            return ([] if ok else ["?"]), "fonts.glyph_coverage_ok"
    except Exception:
        pass
    try:
        if hasattr(mod, "missing_glyphs"):
            paths = getattr(fs, "paths", {}) or {}
            fp = paths.get("regular") or paths.get("cjk")
            miss = mod.missing_glyphs(text, fp)
            return list(miss), "fonts.missing_glyphs"
    except Exception:
        pass
    return None, "unavailable"


# --------------------------------------------------------------------------- #
# scene accessors (defensive: SCENE.md may not exist yet)
# --------------------------------------------------------------------------- #
_SIZE_KEYS = ("size_pt", "font_pt", "font_size_pt", "fontsize_pt", "pt", "size")
_CONTAINER_KEYS = ("container_w_mm", "max_w_mm", "max_width_mm", "wrap_w_mm", "box_w_mm")
_KIND_KEYS = ("kind", "edge_kind", "arrow_kind", "semantic", "relation")
_FILL_KEYS = ("fill", "color", "colour")
_TEXT_KEYS = ("text", "label", "value_text", "string")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def bbox_of(prim):
    b = prim.get("bbox")
    if isinstance(b, dict) and all(_num(b.get(k)) for k in ("x", "y", "w", "h")):
        return b
    c = prim.get("container")
    if isinstance(c, dict) and all(_num(c.get(k)) for k in ("x", "y", "w", "h")):
        return c
    return None


def size_pt_of(prim):
    for k in _SIZE_KEYS:
        v = prim.get(k)
        if _num(v) and 0 < v < 400:
            return float(v)
    st = prim.get("style") if isinstance(prim.get("style"), dict) else {}
    for k in _SIZE_KEYS:
        v = st.get(k)
        if _num(v) and 0 < v < 400:
            return float(v)
    return None


def text_of(prim):
    for k in _TEXT_KEYS:
        v = prim.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def prim_color(prim):
    st = prim.get("style") if isinstance(prim.get("style"), dict) else {}
    for k in _FILL_KEYS:
        for src in (prim, st):
            v = src.get(k)
            rgb = hex_to_rgb(v)
            if rgb is not None:
                return rgb, str(v)
    return None, None


def prim_kind(prim):
    st = prim.get("style") if isinstance(prim.get("style"), dict) else {}
    for k in _KIND_KEYS:
        for src in (prim, st):
            v = src.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def is_arrow_prim(prim):
    """Arrow/relation primitive, in either shape the renderer may publish.

    * type in {arrow, edge, connector}  (contract §4 primitive types), or
    * type == "path" carrying an edge kind (kind / style.kind in scene.EDGE_KINDS).
    """
    if not isinstance(prim, dict):
        return False
    t = prim.get("type")
    if t in ("arrow", "edge", "connector"):
        return True
    if t == "path":
        return prim_kind(prim) in scene.EDGE_KINDS
    return False


def is_page_frame(prim, canvas):
    """A page-sized background frame: layer=='frame' and bbox covers >=99% of the page.

    Such a primitive is a full-bleed background, so it can never satisfy a page
    margin.  Everything else (including any frame that does NOT cover the page)
    stays subject to the margin rule.
    """
    if str((prim or {}).get("layer") or "").lower() != "frame":
        return False
    b = bbox_of(prim)
    if b is None:
        return False
    W, H = float(canvas["w_mm"]), float(canvas["h_mm"])
    page_area = W * H
    if page_area <= 0:
        return False
    inside = (b["x"] >= -1.0 and b["y"] >= -1.0
              and b["x"] + b["w"] <= W + 1.0 and b["y"] + b["h"] <= H + 1.0)
    return inside and (b["w"] * b["h"]) >= 0.99 * page_area


def canvas_of(scene_obj, spec):
    canvas = dict(scene.DEFAULT_CANVAS)
    for src in (((spec or {}).get("meta") or {}).get("canvas") or {}, (scene_obj or {}).get("canvas") or {}):
        for k, v in src.items():
            if _num(v):
                canvas[k] = float(v)
    return canvas


def _text_prims(scene_obj):
    return [p for p in ((scene_obj or {}).get("primitives") or [])
            if isinstance(p, dict) and p.get("type") == "text"]


def norm_ws(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


# --------------------------------------------------------------------------- #
# spec walkers
# --------------------------------------------------------------------------- #
def iter_legends(panel):
    for key in ("legend", "legends"):
        v = panel.get(key)
        if isinstance(v, dict):
            yield v
        elif isinstance(v, list):
            for it in v:
                if isinstance(it, dict):
                    yield it


def iter_source_refs(spec):
    """Yield (json_path, evidence_id) for every element that claims provenance.

    CONTRACT §8.1: ``series.sources`` (list) is equivalent to ``series.source``.
    One tuple is emitted per referenced id, so the counter equals the number of
    references the evidence module actually checks.
    """
    out = []
    for pi, panel in enumerate((spec or {}).get("panels") or []):
        if not isinstance(panel, dict):
            continue
        pp = "$.panels[%d]" % pi
        for ni, node in enumerate(panel.get("nodes") or []):
            if isinstance(node, dict) and node.get("source"):
                out.append(("%s.nodes[%d](%s)" % (pp, ni, node.get("id")), node.get("source")))
        for ei, edge in enumerate(panel.get("edges") or []):
            if isinstance(edge, dict) and edge.get("source"):
                out.append(("%s.edges[%d](%s)" % (pp, ei, edge.get("id")), edge.get("source")))
        for ci, chart in enumerate(panel.get("charts") or []):
            for si, s in enumerate((chart or {}).get("series") or []):
                if not isinstance(s, dict):
                    continue
                refs = s.get("sources")
                if not isinstance(refs, (list, tuple)):
                    refs = [s.get("source")] if s.get("source") else []
                for r in refs:
                    if isinstance(r, str) and r:
                        out.append(("%s.charts[%d].series[%d]" % (pp, ci, si), r))
        for bi, cb in enumerate(panel.get("colorbars") or []):
            if isinstance(cb, dict) and cb.get("source"):
                out.append(("%s.colorbars[%d]" % (pp, bi), cb.get("source")))
        for li, leg in enumerate(iter_legends(panel)):
            for ii, it in enumerate(leg.get("items") or []):
                if isinstance(it, dict) and it.get("source"):
                    out.append(("%s.legend[%d].items[%d]" % (pp, li, ii), it.get("source")))
    return out


def iter_edges(spec):
    out = []
    for pi, panel in enumerate((spec or {}).get("panels") or []):
        if not isinstance(panel, dict):
            continue
        for ei, edge in enumerate(panel.get("edges") or []):
            if isinstance(edge, dict):
                out.append(("%s[%d]" % (panel.get("id"), ei), edge))
    return out


def iter_charts(spec):
    out = []
    for pi, panel in enumerate((spec or {}).get("panels") or []):
        if not isinstance(panel, dict):
            continue
        for ci, chart in enumerate(panel.get("charts") or []):
            if isinstance(chart, dict):
                out.append((panel, chart))
    return out


def iter_all_charts(spec):
    """Every chart in the spec: CONTRACT §6 puts ``charts`` at spec top level,
    the inherited schema nests them in ``panels[]``.  Both must be checked."""
    out = list(iter_charts(spec))
    for ci, chart in enumerate((spec or {}).get("charts") or []):
        if isinstance(chart, dict):
            out.append((None, chart))
    return out


def chart_path(spec, chart):
    """A ``文件:字段`` style location for a chart, for fail messages."""
    cid = chart.get("id") or chart.get("title") or "?"
    for pi, panel in enumerate((spec or {}).get("panels") or []):
        if not isinstance(panel, dict):
            continue
        for ci, c in enumerate(panel.get("charts") or []):
            if c is chart:
                return "$.panels[%d].charts[%d](%s)" % (pi, ci, cid)
    for ci, c in enumerate((spec or {}).get("charts") or []):
        if c is chart:
            return "$.charts[%d](%s)" % (ci, cid)
    return "chart(%s)" % cid


# --------------------------------------------------------------------------- #
# case.json access (the WORK ORDER, authoritative over spec defaults)
# --------------------------------------------------------------------------- #
def _case_json_obj(materials):
    """The parsed ``/input/case.json`` plus where it came from.

    Prefers the ingested material text (works from an output dir alone), then the
    recorded ``case_json_path`` on disk.  Never raises.
    """
    mats = (materials or {}).get("materials")
    if isinstance(mats, list):
        for m in mats:
            if not isinstance(m, dict):
                continue
            p = str(m.get("path") or "").replace("\\", "/")
            if p == "case.json" or p.endswith("/case.json"):
                txt = m.get("text")
                if isinstance(txt, str) and txt.strip():
                    try:
                        return json.loads(txt), "material:case.json"
                    except Exception:
                        pass
    p = (materials or {}).get("case_json_path")
    if isinstance(p, str) and p and os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8-sig") as fh:
                return json.load(fh), "file:%s" % os.path.basename(p)
        except Exception:
            pass
    return None, "absent"


def case_figure(materials):
    """(figure_dict, stale_caption, how) from case.json; ({}, None, ...) if absent."""
    case, how = _case_json_obj(materials)
    if not isinstance(case, dict):
        return {}, None, how
    fig = case.get("figure") if isinstance(case.get("figure"), dict) else {}
    stale = fig.get("stale_caption")
    return fig, (stale if isinstance(stale, str) and stale.strip() else None), how


def requested_canvas(materials, spec):
    """(w_mm, h_mm, source) that the CASE asked for; falls back to the spec.

    CONTRACT §7 H requires the emitted page to match *the request*.  The request
    is ``case.json`` ``figure.canvas`` — comparing only against the spec's own
    ``meta.canvas`` is self-consistency, and silently passes when the pipeline
    ignores the case's canvas entirely (measured on fixture A: case asks
    120x90 mm, emitted page was 200x170 mm, spec asked 200x170 mm -> old H passed).
    """
    fig, _stale, how = case_figure(materials)
    cv = fig.get("canvas") if isinstance(fig.get("canvas"), dict) else {}
    w = cv.get("width_mm", cv.get("w_mm"))
    h = cv.get("height_mm", cv.get("h_mm"))
    if _num(w) and _num(h):
        return float(w), float(h), "case.json(%s).figure.canvas" % how
    if cv:
        return None, None, "case.json(%s).figure.canvas present but w/h unreadable %s" % (how, sorted(cv))
    sc = dict(scene.DEFAULT_CANVAS)
    sc.update({k: float(v) for k, v in (((spec or {}).get("meta") or {}).get("canvas") or {}).items()
               if _num(v)})
    return sc["w_mm"], sc["h_mm"], "spec.meta.canvas (no case.json figure.canvas)"


# --------------------------------------------------------------------------- #
# gate A — canvas / margins
# --------------------------------------------------------------------------- #
def gate_a(spec, scene_obj, export_report, out_dir, materials):
    canvas = canvas_of(scene_obj, spec)
    W, H, margin = canvas["w_mm"], canvas["h_mm"], canvas.get("margin_mm", 3.0)
    prims = (scene_obj or {}).get("primitives") or []
    if not prims:
        return "fail", ("page %.1fx%.1f mm margin %.2f mm: 0 primitives in scene.json, "
                        "0 measured, containment UNVERIFIABLE (build did not publish a scene)"
                        % (W, H, margin))
    worst, viol, nobbox, exempt = None, 0, 0, 0
    for p in prims:
        if not isinstance(p, dict):
            continue
        b = bbox_of(p)
        if b is None:
            nobbox += 1
            continue
        if is_page_frame(p, canvas):
            exempt += 1
            continue
        over = max(margin - b["x"], margin - b["y"],
                   (b["x"] + b["w"]) - (W - margin), (b["y"] + b["h"]) - (H - margin))
        if over > BBOX_TOL_MM:
            viol += 1
            if worst is None or over > worst[1]:
                worst = (str(p.get("id", "?")), over)
    frame_note = "%d page-sized frame exempt" % exempt
    if viol:
        return "fail", ("page %.2fx%.2f mm margin %.2f mm, primitives=%d (no bbox: %d, %s): "
                        "%d outside, worst=%s +%.3f mm overshoot"
                        % (W, H, margin, len(prims), nobbox, frame_note, viol, worst[0], worst[1]))
    if nobbox == len(prims):
        return "warn", ("page %.2fx%.2f mm margin %.2f mm: %d primitives, none carries a bbox "
                        "(%s) -> containment UNVERIFIABLE" % (W, H, margin, len(prims), frame_note))
    return "pass", ("page %.2fx%.2f mm margin %.2f mm, primitives=%d (no bbox: %d, %s): "
                    "violations=0, worst margin=%+.3f mm"
                    % (W, H, margin, len(prims), nobbox, frame_note, -margin))


# --------------------------------------------------------------------------- #
# gate B — typography
# --------------------------------------------------------------------------- #
def gate_b(spec, scene_obj, export_report, out_dir, materials):
    fs = resolve_fs(spec)
    texts = _text_prims(scene_obj)
    if not texts:
        return "fail", "0 text primitives in scene.json -> font size/width UNVERIFIABLE (measured 0)"

    panel_titles = [norm_ws(p.get("title")) for p in (spec.get("panels") or []) if isinstance(p, dict)]
    meta = (spec or {}).get("meta") or {}
    main_titles = [norm_ws(t) for t in [meta.get("title"), ((spec or {}).get("texts") or {}).get("title")] if t]

    def classify(prim, t):
        role = str(prim.get("role") or prim.get("layer") or "").lower()
        if ("main" in role and "title" in role) or role in ("title", "figure_title", "main_title"):
            return "main_title"
        if ("panel" in role and "title" in role) or role == "panel_title":
            return "panel_title"
        if t in main_titles:
            return "main_title"
        if t in panel_titles:
            return "panel_title"
        return "body"

    limits = {"body": scene.MIN_BODY_PT, "panel_title": scene.MIN_PANEL_TITLE_PT,
              "main_title": scene.MIN_MAIN_TITLE_PT}
    min_seen = {k: None for k in limits}
    below = []
    overflow = []
    unknown_size = 0
    missing_glyphs = {}
    truncated = []
    glyph_src = "unavailable"
    widths_src = set()
    checked_text = []
    for p in texts:
        t = text_of(p)
        if t is None:
            continue
        checked_text.append(t)
        sp = size_pt_of(p)
        if sp is None:
            unknown_size += 1
            continue
        cls = classify(p, norm_ws(t))
        lim = limits[cls]
        min_seen[cls] = sp if min_seen[cls] is None else min(min_seen[cls], sp)
        if sp + 1e-9 < lim:
            below.append((str(p.get("id", "?")), cls, round(sp, 2), lim))
        lines = p.get("lines") if isinstance(p.get("lines"), list) else None
        if lines:
            mw = max(measure_width_mm(str(ln), sp, fs, bool(p.get("bold")))[0] for ln in lines)
            widths_src.add(measure_width_mm(str(lines[0]), sp, fs, bool(p.get("bold")))[1])
            rendered = norm_ws(" ".join(str(ln) for ln in lines))
            declared_tokens = norm_ws(t).split()
            dropped = [tok for tok in declared_tokens if tok not in rendered.split()]
            if dropped:
                truncated.append((str(p.get("id", "?")), len(dropped), dropped[:4]))
        else:
            mw, src = measure_width_mm(t, sp, fs, bool(p.get("bold")))
            widths_src.add(src)
        container, ckey = None, "none"
        for k in _CONTAINER_KEYS:
            if _num(p.get(k)):
                container, ckey = float(p[k]), k
                break
        if container is None and isinstance(p.get("container"), dict) and _num(p["container"].get("w")):
            container, ckey = float(p["container"]["w"]), "container.w"
        if container is None:
            b = bbox_of(p)
            if b is not None:
                container, ckey = b["w"], "bbox.w"
        if container is not None and mw - container > 0.05:
            overflow.append((str(p.get("id", "?")), round(mw, 2), round(container, 2), ckey))
        miss, gsrc = glyph_coverage(t, fs)
        glyph_src = gsrc
        if miss:
            missing_glyphs[str(p.get("id", "?"))] = miss[:4]

    def _mn(k):
        v = min_seen[k]
        return "n/a" if v is None else "%.2fpt" % v
    parts = ["text_nodes=%d" % len(texts),
             "min_body=%s(>=%.1f)" % (_mn("body"), limits["body"]),
             "min_panel_title=%s(>=%.1f)" % (_mn("panel_title"), limits["panel_title"]),
             "min_main_title=%s(>=%.1f)" % (_mn("main_title"), limits["main_title"]),
             "below_floor=%d" % len(below),
             "overflow_events=%d" % len(overflow),
             "truncated_labels=%d" % len(truncated),
             "unknown_size=%d" % unknown_size,
             "glyph_missing=%d(items)" % len(missing_glyphs),
             "widths=%s" % ("+".join(sorted(widths_src)) or "n/a")]
    if overflow:
        o = max(overflow, key=lambda x: x[1] - x[2])
        parts.append("worst_overflow=%s %.2fmm > %.2fmm (%s) +%.2fmm" % (o[0], o[1], o[2], o[3], o[1] - o[2]))
    if below:
        b0 = min(below, key=lambda x: x[2])
        parts.append("worst_size=%s %s %.2fpt < %.1fpt" % (b0[0], b0[1], b0[2], b0[3]))
    if glyph_src == "unavailable":
        parts.append("glyph_check=unavailable")
    if truncated:
        t0 = truncated[0]
        parts.append("worst_truncation=%s %d token(s) dropped: %s" % (t0[0], t0[1], t0[2]))
    detail = ", ".join(parts)
    if missing_glyphs:
        first = sorted(missing_glyphs)[0]
        return "fail", detail + " | missing in %s: %s" % (first, missing_glyphs[first])
    if below or overflow or truncated:
        return "fail", detail
    if unknown_size and unknown_size == len([p for p in texts if text_of(p) is not None]):
        return "warn", detail + " | no font size published on any text primitive"
    if glyph_src == "unavailable":
        return "warn", detail
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate C — colour accessibility
# --------------------------------------------------------------------------- #
def _colour_groups(spec, scene_obj):
    """[(label, [hex, ...])] — colours that encode alternatives within one panel."""
    groups = []
    for pi, panel in enumerate((spec or {}).get("panels") or []):
        if not isinstance(panel, dict):
            continue
        pid = panel.get("id") or "P%d" % pi
        for li, leg in enumerate(iter_legends(panel)):
            cols = [it.get("color") for it in (leg.get("items") or []) if isinstance(it, dict)]
            cols = [c for c in cols if hex_to_rgb(c)]
            if len(cols) > 1:
                groups.append(("%s.legend[%d]" % (pid, li), cols))
        for ci, chart in enumerate(panel.get("charts") or []):
            cols = [s.get("color") for s in (chart.get("series") or []) if isinstance(s, dict)]
            cols = [c for c in cols if hex_to_rgb(c)]
            if len(cols) > 1:
                groups.append(("%s.chart[%s]" % (pid, chart.get("id") or ci), cols))
    # scene-level legend/chart primitives, when render-core publishes colours
    for p in (scene_obj or {}).get("primitives") or []:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "legend" and isinstance(p.get("items"), list):
            cols = [it.get("color") or it.get("fill") for it in p["items"] if isinstance(it, dict)]
            cols = [c for c in cols if hex_to_rgb(c)]
            if len(cols) > 1:
                groups.append(("scene:legend:%s" % p.get("id", "?"), cols))
        elif t == "chart" and isinstance(p.get("series"), list):
            cols = [s.get("color") or s.get("fill") for s in p["series"] if isinstance(s, dict)]
            cols = [c for c in cols if hex_to_rgb(c)]
            if len(cols) > 1:
                groups.append(("scene:chart:%s" % p.get("id", "?"), cols))
    return groups


_PAINTED = ("rect", "rrect", "circle", "ellipse", "path", "polygon", "capsule", "hexagon",
            "diamond", "note", "panel", "chart", "colorbar", "legend", "scalebar", "frame")


def _text_background_pairs(spec, scene_obj):
    """[(element_id, fg_hex, bg_hex, fg_rgb, bg_rgb, size_pt, bold, bg_source)].

    The background of a label is the *topmost painted primitive that contains it*
    (a chip/panel fill), falling back to the page background.  Using the page
    background for every label would score white text on a dark chip as 1:1.
    """
    canvas = canvas_of(scene_obj, spec)
    prims = [p for p in ((scene_obj or {}).get("primitives") or []) if isinstance(p, dict)]
    page_area = canvas["w_mm"] * canvas["h_mm"]
    bg_rgb, bg_raw, bg_src = None, None, "page-default"
    for p in prims:
        if p.get("type") != "rect" and str(p.get("layer") or "").lower() != "frame":
            continue
        b = bbox_of(p)
        if not b or page_area <= 0:
            continue
        if (b["w"] * b["h"]) >= 0.9 * page_area:
            rgb, raw = prim_color(p)
            if rgb is not None:
                bg_rgb, bg_raw, bg_src = rgb, raw, str(p.get("id", "page-rect"))
                break
    if bg_rgb is None:
        bg_raw = (((spec or {}).get("meta") or {}).get("background")
                  or ((spec or {}).get("palette") or {}).get("background") or "#ffffff")
        bg_rgb = hex_to_rgb(bg_raw) or (1.0, 1.0, 1.0)
        bg_raw = bg_raw if hex_to_rgb(bg_raw) else "#ffffff(default)"
    painted = []
    for p in prims:
        if p.get("type") not in _PAINTED:
            continue
        b = bbox_of(p)
        if not b:
            continue
        rgb, raw = prim_color(p)
        if rgb is None:
            continue
        painted.append((p, b, rgb, raw))
    pairs = []
    for p in _text_prims(scene_obj):
        fg, fg_raw = prim_color(p)
        if fg is None:
            continue
        tb = bbox_of(p)
        bgc, bgr_raw, src = bg_rgb, bg_raw, bg_src
        if tb is not None:
            cx = tb["x"] + tb["w"] / 2.0
            cy = tb["y"] + tb["h"] / 2.0
            for q, b, rgb, raw in painted:      # later primitives paint on top
                if q is p:
                    continue
                if b["x"] - 1e-6 <= cx <= b["x"] + b["w"] + 1e-6 and b["y"] - 1e-6 <= cy <= b["y"] + b["h"] + 1e-6:
                    if (b["w"] * b["h"]) >= 0.9 * page_area:
                        continue
                    bgc, bgr_raw, src = rgb, raw, str(q.get("id", "?"))
        pairs.append((str(p.get("id", "?")), fg_raw, bgr_raw, fg, bgc, size_pt_of(p),
                      bool(p.get("bold")), src))
    return pairs


def gate_c(spec, scene_obj, export_report, out_dir, materials):
    groups = _colour_groups(spec, scene_obj)
    palette = scene.resolve_palette(spec)
    pal_rgb = [hex_to_rgb(c) for c in palette if hex_to_rgb(c)]
    stats = []
    if len(pal_rgb) > 1:
        for kind in ("normal", "protan", "deutan", "tritan"):
            sim = pal_rgb if kind == "normal" else [simulate(c, kind) for c in pal_rgb]
            labs = [to_lab(c) for c in sim]
            m = min(delta_e(labs[i], labs[j]) for i, j in itertools.combinations(range(len(labs)), 2))
            stats.append("palette_%s_min_dE=%.2f" % (kind, m))
    if groups:
        worst_cvd = None
        worst_gray = None
        for label, cols in groups:
            rgbs = [hex_to_rgb(c) for c in cols]
            for kind in ("normal", "protan", "deutan", "tritan"):
                sim = rgbs if kind == "normal" else [simulate(c, kind) for c in rgbs]
                labs = [to_lab(c) for c in sim]
                for i, j in itertools.combinations(range(len(labs)), 2):
                    d = delta_e(labs[i], labs[j])
                    if worst_cvd is None or d < worst_cvd[0]:
                        worst_cvd = (d, label, kind, cols[i], cols[j])
            ls = [to_lab(c)[0] for c in rgbs]
            for i, j in itertools.combinations(range(len(ls)), 2):
                dl = abs(ls[i] - ls[j])
                if worst_gray is None or dl < worst_gray[0]:
                    worst_gray = (dl, label, cols[i], cols[j])
        stats.append("categorical_groups=%d" % len(groups))
        stats.append("worst_CVD_min_dE=%.2f(%s/%s %s vs %s, floor %.1f)"
                     % (worst_cvd[0], worst_cvd[2], worst_cvd[1], worst_cvd[3], worst_cvd[4], CVD_MIN_DE))
        stats.append("worst_gray_min_dL*=%.2f(%s %s vs %s, floor %.1f)"
                     % (worst_gray[0], worst_gray[1], worst_gray[2], worst_gray[3], GRAY_MIN_DL))
    else:
        stats.append("categorical_groups=0")

    pairs = _text_background_pairs(spec, scene_obj)
    worst_ct = None
    ct_fail = []
    for eid, fg_raw, bg_raw, fg, bg, sp, bold, bg_src in pairs:
        cr = contrast_ratio(fg, bg)
        large = (sp is not None and sp >= LARGE_PT) or (sp is not None and bold and sp >= LARGE_BOLD_PT)
        need = WCAG_LARGE if large else WCAG_NORMAL
        if worst_ct is None or cr < worst_ct[0]:
            worst_ct = (cr, eid, fg_raw, bg_raw, need, bg_src)
        if cr + 1e-9 < need:
            ct_fail.append((eid, round(cr, 2), need))
    if pairs:
        bg_counts = {}
        for _, _, _, _, _, _, _, s in pairs:
            bg_counts[s] = bg_counts.get(s, 0) + 1
        stats.append("text_bg_pairs=%d(backgrounds %d: %s)"
                     % (len(pairs), len(bg_counts),
                        ",".join("%s:%d" % kv for kv in sorted(bg_counts.items())[:4])))
        stats.append("worst_contrast=%.2f:1(%s fg=%s on bg=%s[%s] need>=%.1f)"
                     % (worst_ct[0], worst_ct[1], worst_ct[2], worst_ct[3], worst_ct[5], worst_ct[4]))
        stats.append("contrast_below=%d" % len(ct_fail))
    else:
        stats.append("text_bg_pairs=0")

    detail = ", ".join(stats)
    if not groups and not pairs:
        return "warn", detail + " | no colour-coded categories and no coloured text -> UNVERIFIABLE"
    fails = []
    if groups and worst_cvd[0] + 1e-9 < CVD_MIN_DE:
        fails.append("CVD dE %.2f < %.1f (%s/%s)" % (worst_cvd[0], CVD_MIN_DE, worst_cvd[1], worst_cvd[2]))
    if groups and worst_gray[0] + 1e-9 < GRAY_MIN_DL:
        fails.append("grayscale dL* %.2f < %.1f (%s)" % (worst_gray[0], GRAY_MIN_DL, worst_gray[1]))
    if ct_fail:
        fails.append("contrast %d below WCAG" % len(ct_fail))
    if fails:
        return "fail", detail + " | " + "; ".join(fails)
    if not groups or not pairs:
        return "warn", detail + " | partial evidence (groups=%d, text_bg_pairs=%d)" % (len(groups), len(pairs))
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate D — encoding fidelity (legend / colorbar / units / arrows)
# --------------------------------------------------------------------------- #
def _panel_data_values(panel):
    """Values a panel's colour channels encode, grouped by unit.

    A colourbar titled "(fold)" cannot encode pmol/min readings, so comparing its
    range against every value in the panel is wrong: it reports a mismatch for
    data the bar does not claim to describe.
    """
    vals = []
    for node in panel.get("nodes") or []:
        if isinstance(node, dict) and _num(node.get("value")):
            vals.append((float(node["value"]), node.get("unit")))
    for edge in panel.get("edges") or []:
        if isinstance(edge, dict) and _num(edge.get("value")):
            vals.append((float(edge["value"]), edge.get("unit")))
    return vals


def gate_d(spec, scene_obj, export_report, out_dir, materials):
    problems, notes, unverified = [], [], []
    legend_checked = 0
    colorbar_checked = 0
    unit_missing = []
    colour_pairs = 0
    for pi, panel in enumerate((spec or {}).get("panels") or []):
        if not isinstance(panel, dict):
            continue
        pid = panel.get("id") or "P%d" % pi
        charts = [(c.get("id") or "chart%d" % i, [s.get("name") for s in (c.get("series") or [])
                                                   if isinstance(s, dict)])
                  for i, c in enumerate(panel.get("charts") or []) if isinstance(c, dict)]
        charts = [(cid, names) for cid, names in charts if names]
        # CONTRACT 8.7: a legend's label set == the union of series names of the
        # panel's charts.  CONTRACT 6 D: that union must also equal a chart's
        # category set (multiset) — i.e. the colours encode one chart's categories.
        for li, leg in enumerate(iter_legends(panel)):
            items = [it.get("label") for it in (leg.get("items") or []) if isinstance(it, dict)]
            items = [x for x in items if x]
            if not items:
                continue
            legend_checked += 1
            want = sorted(x.strip().casefold() for x in items)
            if not charts:
                unverified.append("%s.legend[%d]: %d items but 0 chart series -> UNVERIFIABLE"
                             % (pid, li, len(want)))
                continue
            union = sorted({n.strip().casefold() for cid, names in charts for n in names if n})
            exact = [cid for cid, names in charts
                     if sorted(n.strip().casefold() for n in names if n) == want]
            allnames = [n.strip().casefold() for cid, names in charts for n in names if n]
            orphans = sorted(x for x in want if x not in allnames)
            if orphans:
                problems.append("%s.legend[%d] has %d orphan item(s) not in any chart series: %s"
                                % (pid, li, len(orphans), orphans[:4]))
            if union != want:
                problems.append("%s.legend[%d] %d items != union of series names in %d chart(s) %s "
                                "(CONTRACT 8.7) missing=%s extra=%s"
                                % (pid, li, len(want), len(charts), [c for c, _ in charts],
                                   sorted(set(union) - set(want)), sorted(set(want) - set(union))))
            elif len(charts) == 1:
                # a single colour-coded chart must also match as a multiset (6 D)
                if sorted(n.strip().casefold() for n in charts[0][1] if n) != want:
                    problems.append("%s.legend[%d] %d items != multiset of chart %s series %s"
                                    % (pid, li, len(want), charts[0][0], charts[0][1]))
                else:
                    notes.append("%s.legend[%d] %d items == series union == chart %s series (multiset)"
                                 % (pid, li, len(want), charts[0][0]))
            else:
                exact_txt = ""
                if exact:
                    exact_txt = ", equals chart %s series exactly" % ",".join(exact)
                notes.append("%s.legend[%d] %d items == union of series names across %d chart(s)%s "
                             "(no single chart has this multiset; 8.7 set rule satisfied)"
                             % (pid, li, len(want), len(charts), exact_txt))

        # a colour-coded chart with no legend cannot be read off the figure
        legs_here = list(iter_legends(panel))
        if charts and not legs_here:
            problems.append("%s has %d chart(s) with %d series name(s) but 0 legend -> colour channel unmapped"
                            % (pid, len(charts),
                               len({n.strip().casefold() for _, names in charts for n in names if n})))
        leg_colors, ser_colors = {}, {}
        for leg in legs_here:
            for it in leg.get("items") or []:
                if isinstance(it, dict) and it.get("label") and it.get("color"):
                    leg_colors[str(it["label"]).strip().casefold()] = str(it["color"]).strip().lower()
        for c in panel.get("charts") or []:
            for s in (c.get("series") or []):
                if isinstance(s, dict) and s.get("name") and s.get("color"):
                    ser_colors.setdefault(str(s["name"]).strip().casefold(), str(s["color"]).strip().lower())
        colour_pairs += len(set(leg_colors) & set(ser_colors))
        mismatch = sorted(k for k in leg_colors if k in ser_colors and leg_colors[k] != ser_colors[k])
        if mismatch:
            problems.append("%s legend colour != series colour for %d name(s): %s"
                            % (pid, len(mismatch),
                               ["%s %s!=%s" % (k, leg_colors[k], ser_colors[k]) for k in mismatch[:3]]))

        # colourbar vs the values it encodes (node/edge values, else heatmap data)
        for bi, cb in enumerate(panel.get("colorbars") or []):
            if not isinstance(cb, dict):
                continue
            if not cb.get("unit"):
                unit_missing.append("%s.colorbar[%s].unit" % (pid, cb.get("id")))
            vmin, vmax = cb.get("vmin"), cb.get("vmax")
            if not (_num(vmin) and _num(vmax)):
                problems.append("%s.colorbar[%s] vmin/vmax not numeric" % (pid, cb.get("id")))
                continue
            heat = [c for c in (panel.get("charts") or [])
                    if isinstance(c, dict) and c.get("type") == "heatmap"]
            vals = []
            for h in heat:
                for s in h.get("series") or []:
                    vals += [float(v) for v in (s.get("values") or []) if _num(v)]
            src_desc = "heatmap"
            if not vals:
                pairs = _panel_data_values(panel)
                cb_unit = (cb.get("unit") or "").strip().lower()
                # only values whose unit matches the bar's own unit belong to it
                same = [v for v, u in pairs
                        if not cb_unit or (u or "").strip().lower() == cb_unit]
                vals = same or [v for v, _ in pairs]
                src_desc = "node/edge values (unit %s)" % (cb.get("unit") or "?")
            if not vals:
                unverified.append("%s.colorbar[%s] vmin=%.3f vmax=%.3f: 0 data values in panel -> UNVERIFIABLE"
                             % (pid, cb.get("id"), vmin, vmax))
                continue
            colorbar_checked += 1
            dmin, dmax = min(vals), max(vals)
            span = max(vmax - vmin, 1e-12)
            allowed = max(COLORBAR_SLACK_FRAC * span, 0.05 * max(dmax - dmin, 1e-12))
            slack_low, slack_high = dmin - vmin, vmax - dmax
            if slack_low < -1e-9 or slack_high < -1e-9:
                problems.append("%s.colorbar[%s] declared %.3f..%.3f excludes %s min=%.3f max=%.3f"
                                % (pid, cb.get("id"), vmin, vmax, src_desc, dmin, dmax))
            elif max(slack_low, slack_high) > allowed + 1e-9:
                problems.append("%s.colorbar[%s] declared %.3f..%.3f too loose for %s %.3f..%.3f "
                                "(slack %.3f>%.3f)"
                                % (pid, cb.get("id"), vmin, vmax, src_desc, dmin, dmax,
                                   max(slack_low, slack_high), allowed))
            else:
                notes.append("%s.colorbar[%s] %.3f..%.3f covers %s %.3f..%.3f (slack %.3f/%.3f<=%.3f)"
                             % (pid, cb.get("id"), vmin, vmax, src_desc, dmin, dmax,
                                slack_low, slack_high, allowed))

    # units on every chart
    for panel, chart in iter_charts(spec):
        if not chart.get("unit"):
            unit_missing.append("%s.chart[%s].unit" % (panel.get("id"), chart.get("id")))

    # arrow direction counts vs spec edge kinds
    spec_kinds = {}
    for label, edge in iter_edges(spec):
        k = edge.get("kind") or "unspecified"
        spec_kinds[k] = spec_kinds.get(k, 0) + 1
    scene_arrows = [p for p in (scene_obj or {}).get("primitives") or [] if is_arrow_prim(p)]
    scene_kinds = {}
    for p in scene_arrows:
        k = prim_kind(p) or "unspecified"
        scene_kinds[k] = scene_kinds.get(k, 0) + 1
    spec_total = sum(spec_kinds.values())
    scene_total = len(scene_arrows)
    if not scene_arrows and spec_total:
        problems.append("spec has %d edges %s but scene has 0 arrow primitives" % (spec_total, spec_kinds))
    elif scene_kinds != spec_kinds and "unspecified" not in scene_kinds:
        problems.append("scene arrow kinds %s != spec edge kinds %s" % (scene_kinds, spec_kinds))
    elif scene_total != spec_total:
        problems.append("scene arrow count %d != spec edge count %d" % (scene_total, spec_total))

    # Per-edge direction/wiring. A histogram cannot tell a reversed arrow from a
    # correct one: swapping every edge's from/to keeps the counts identical while
    # the causal arrows point backwards, which is the single worst error a
    # mechanism figure can make.
    direction_checked = direction_bad = 0
    if scene_arrows and any("from" in p or "to" in p for p in scene_arrows):
        spec_pairs = {}
        for label, edge in iter_edges(spec):
            a, b = edge.get("from"), edge.get("to")
            if isinstance(a, str) and isinstance(b, str):
                spec_pairs[(a, b)] = edge.get("kind") or "unspecified"
        scene_pairs = {}
        for p in scene_arrows:
            a, b = p.get("from"), p.get("to")
            if isinstance(a, str) and isinstance(b, str):
                scene_pairs[(a, b)] = prim_kind(p) or "unspecified"
        direction_checked = len(scene_pairs)
        for (a, b), kind in scene_pairs.items():
            if (a, b) not in spec_pairs:
                if (b, a) in spec_pairs:
                    problems.append("arrow %s->%s is REVERSED (spec says %s->%s)" % (a, b, b, a))
                else:
                    problems.append("arrow %s->%s matches no spec edge" % (a, b))
                direction_bad += 1
            elif spec_pairs[(a, b)] != kind:
                problems.append("arrow %s->%s kind %r != spec %r" % (a, b, kind, spec_pairs[(a, b)]))
                direction_bad += 1
        for pair in spec_pairs:
            if pair not in scene_pairs:
                problems.append("spec edge %s->%s has no arrow in the scene" % pair)
                direction_bad += 1

    if unit_missing:
        problems.append("%d element(s) missing unit: %s" % (len(unit_missing), unit_missing[:5]))

    detail = ("legends_checked=%d, legend_series_colour_pairs=%d, colorbars_checked=%d, spec_edges=%d%s, "
              "scene_arrows=%d%s, edge_direction_checked=%d, units_missing=%d, problems=%d%s"
              % (legend_checked, colour_pairs, colorbar_checked, spec_total,
                 "{%s}" % ",".join("%s:%d" % kv for kv in sorted(spec_kinds.items())),
                 scene_total,
                 "{%s}" % ",".join("%s:%d" % kv for kv in sorted(scene_kinds.items())),
                 direction_checked, len(unit_missing),
                 len(problems),
                 (" | " + "; ".join(problems[:3])) if problems else (" | " + "; ".join(notes[:3]))))
    detail_unverified = "; ".join(unverified[:3])
    if problems:
        return "fail", detail
    # `unverified` = a comparison that could not be made (soft UNVERIFIABLE).
    # `notes` = a comparison that WAS made and matched; those are informational
    # and must not downgrade the gate, otherwise a legend can never pass.
    if unverified and not (legend_checked or colorbar_checked):
        return "warn", detail
    if legend_checked == 0 and colorbar_checked == 0:
        return "warn", detail + " | no legend and no bound colorbar -> UNVERIFIABLE"
    if unverified:
        return "warn", detail
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate E — provenance
# --------------------------------------------------------------------------- #
def _material_texts(materials):
    out = []
    mats = (materials or {}).get("materials")
    if not isinstance(mats, list):
        mats = materials if isinstance(materials, list) else []
    for m in mats:
        if not isinstance(m, dict):
            continue
        text = m.get("text")
        if not isinstance(text, str) or not text:
            pages = m.get("pages")
            if isinstance(pages, list):
                text = "\n".join(p if isinstance(p, str) else str(p) for p in pages)
        if not isinstance(text, str):
            text = ""
        out.append({"path": str(m.get("path") or ""), "text": text, "kind": m.get("kind")})
    return out


def _pick_material(src_file, mats):
    norm = str(src_file or "").replace("\\", "/")
    for m in mats:
        if m["path"].replace("\\", "/") == norm:
            return m, "exact"
    for m in mats:
        if m["path"].replace("\\", "/").endswith("/" + norm) or m["path"].replace("\\", "/") == norm:
            return m, "relative"
    for m in mats:
        if os.path.basename(m["path"].replace("\\", "/")) == os.path.basename(norm):
            return m, "basename"
    return None, "missing"


def _locator_span(locator, text):
    """Resolve 'L21-L22' / 'L7' / 'row 9' to (start,end) char offsets, or None."""
    loc = str(locator or "").strip()
    lines = text.splitlines(keepends=True)
    m = re.match(r"^L(\d+)(?:\s*-\s*L?(\d+))?$", loc)
    if m:
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a < 1 or b > len(lines) or a > b:
            return None, "line span out of range (file has %d lines)" % len(lines)
        start = sum(len(x) for x in lines[:a - 1])
        end = sum(len(x) for x in lines[:b])
        return (start, end), None
    m = re.match(r"^rows?\s+(\d+)(?:\s*-\s*(\d+))?$", loc, re.I)
    if m:
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a < 1 or b > len(lines) or a > b:
            return None, "row span out of range (file has %d lines)" % len(lines)
        start = sum(len(x) for x in lines[:a - 1])
        end = sum(len(x) for x in lines[:b])
        return (start, end), None
    return None, "unrecognised locator"


def _own_provenance(spec, materials):
    """Identical semantics to evidence.check_provenance, measured by us."""
    mats = _material_texts(materials)
    ev_items = [e for e in ((spec or {}).get("evidence") or []) if isinstance(e, dict)]
    ev_ids = {e.get("id") for e in ev_items}
    refs = iter_source_refs(spec)
    dangling = [(path, ref) for path, ref in refs if ref not in ev_ids]
    empty_mats = len(mats) == 0 or all(not m["text"] for m in mats)
    resolved, unresolved = [], []
    for ev in ev_items:
        src = ev.get("source") or {}
        quote = norm_ws(src.get("quote"))
        loc = src.get("locator")
        if empty_mats:
            unresolved.append({"id": ev.get("id"), "reason": "no material text available"})
            continue
        mat, how = _pick_material(src.get("file"), mats)
        if mat is None or not mat["text"]:
            unresolved.append({"id": ev.get("id"), "reason": "source.file %r not in materials (%s)"
                               % (src.get("file"), how)})
            continue
        hay = norm_ws(mat["text"])
        if not quote or quote not in hay:
            unresolved.append({"id": ev.get("id"), "reason": "quote (%d chars) not found verbatim in %s"
                               % (len(quote), os.path.basename(mat["path"]))})
            continue
        span, why = _locator_span(loc, mat["text"])
        if span is None:
            resolved.append({"id": ev.get("id"), "file": mat["path"], "locator": loc,
                             "mode": "quote-verbatim, locator unresolved (%s)" % why})
            continue
        seg = norm_ws(mat["text"][span[0]:span[1]])
        if quote in seg:
            resolved.append({"id": ev.get("id"), "file": mat["path"], "locator": loc, "mode": "span"})
        else:
            unresolved.append({"id": ev.get("id"),
                               "reason": "quote found in file but outside locator %s span" % loc})
    return {"refs": len(refs), "dangling": dangling, "resolved": resolved,
            "unresolved": unresolved, "empty_materials": empty_mats, "materials": len(mats)}


def gate_e(spec, scene_obj, export_report, out_dir, materials):
    own = _own_provenance(spec, materials)
    mod = _soft_import("evidence")
    mod_note = "own"
    mod_issues = []
    if mod is not None and hasattr(mod, "check_provenance"):
        try:
            mod_issues = mod.check_provenance(spec, materials) or []
            mod_note = "evidence.check_provenance"
        except Exception as exc:
            mod_note = "evidence.check_provenance raised (%s); fell back to own" % type(exc).__name__
    mod_err = [i for i in mod_issues if isinstance(i, dict) and i.get("severity") == "error"]
    mod_warn = [i for i in mod_issues if isinstance(i, dict) and i.get("severity") != "error"]
    detail = ("source_checks=%s, materials=%d, refs=%d, dangling=%d, evidence_items=%d, "
              "quotes_resolved=%d, quotes_unresolved=%d, module_issues=%d(err=%d,warn=%d)"
              % (mod_note, own["materials"], own["refs"], len(own["dangling"]),
                 len(((spec or {}).get("evidence") or [])), len(own["resolved"]),
                 len(own["unresolved"]), len(mod_issues), len(mod_err), len(mod_warn)))
    if own["dangling"]:
        d0 = own["dangling"][0]
        return "fail", detail + " | dangling source ids, e.g. %s -> %r" % (d0[0], d0[1])
    if own["empty_materials"]:
        return "warn", detail + " | UNVERIFIABLE: 0 material text, %d quotes unchecked" % len(own["unresolved"])
    if own["unresolved"]:
        u0 = own["unresolved"][0]
        return "fail", detail + " | first unresolved: %s (%s)" % (u0.get("id"), u0.get("reason"))
    if mod_err:
        return "fail", detail + " | module error: %s" % (mod_err[0].get("message") or mod_err[0])
    if mod_warn:
        return "warn", detail + " | module warn: %s" % (mod_warn[0].get("message") or mod_warn[0])
    if own["refs"] == 0 and len(((spec or {}).get("evidence") or [])) == 0:
        return "warn", detail + " | no provenance claims to check"
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate F — editability
# --------------------------------------------------------------------------- #
def _localname(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _svg_labels(spec, scene_obj):
    """Labels the emitted SVG must carry as real <text>.

    Returns (required, advisory) where
        required = {normalised_label: {"origin": str, "panel": panel_id|None}}
        advisory = {normalised_label: {"origin": str}}   (free prose, reported only)

    required covers every label the scene actually typeset (text primitives) plus
    node labels / panel titles / legend items from the spec.
    """
    required, advisory = {}, {}

    def _add(target, label, origin, panel=None):
        key = norm_ws(label)
        if key and key not in target:
            target[key] = {"origin": origin, "panel": panel}

    for p in _text_prims(scene_obj):
        t = text_of(p)
        if t:
            _add(required, t, "scene:%s" % p.get("id", "?"), p.get("panel"))
    for pi, panel in enumerate((spec or {}).get("panels") or []):
        if not isinstance(panel, dict):
            continue
        pid = panel.get("id")
        for node in panel.get("nodes") or []:
            if isinstance(node, dict) and node.get("label"):
                _add(required, node["label"], "node:%s" % node.get("id"), pid)
        if panel.get("title"):
            _add(required, panel["title"], "panel:%s" % pid, pid)
        for leg in iter_legends(panel):
            for it in leg.get("items") or []:
                if isinstance(it, dict) and it.get("label"):
                    _add(required, it["label"], "legend:%s" % pid, pid)
    for key in ("title", "subtitle", "caption", "conclusion", "summary"):
        v = ((spec or {}).get("texts") or {}).get(key)
        if v:
            _add(advisory, v, "text:%s" % key)
    for k in list(advisory):
        if k in required:
            advisory.pop(k)
    return required, advisory


def gate_f(spec, scene_obj, export_report, out_dir, materials):
    svg_path = os.path.join(out_dir, "figure.svg")
    if not os.path.exists(svg_path):
        return "fail", "figure.svg missing at %s (0 bytes) -> editability UNVERIFIABLE" % svg_path
    with open(svg_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return "fail", "figure.svg does not parse: %s (bytes=%d)" % (exc, len(raw.encode('utf-8')))

    text_nodes, groups, all_ids, images = [], [], [], []
    for el in root.iter():
        ln = _localname(el.tag)
        if ln == "text":
            parts = [el.text] if (el.text or "").strip() else []
            for child in el:
                if _localname(child.tag) == "tspan":
                    parts.append("".join(child.itertext()))
            content = " ".join(p for p in parts if p and p.strip()) if parts else "".join(el.itertext())
            if content.strip():
                text_nodes.append(content)
        elif ln == "g":
            groups.append(el.get("id"))
        elif ln == "image":
            images.append(el)
        i = el.get("id")
        if i is not None:
            all_ids.append(i)
    dups = sorted({i for i in all_ids if all_ids.count(i) > 1})
    named = sum(1 for g in groups if g)
    unnamed = len(groups) - named

    # full-page rasterisation check
    canvas = canvas_of(scene_obj, spec)
    page_area = canvas["w_mm"] * canvas["h_mm"]
    full_raster = 0
    for el in images:
        def _len_mm(v):
            if not isinstance(v, str):
                return None
            m = re.match(r"^\s*([\d.]+)\s*(mm|cm|in|pt|px)?", v)
            if not m:
                return None
            val = float(m.group(1))
            unit = m.group(2) or "px"
            return {"mm": val, "cm": val * 10, "in": val * 25.4, "pt": val * 25.4 / 72, "px": val * 25.4 / 96}[unit]
        w = _len_mm(el.get("width"))
        h = _len_mm(el.get("height"))
        if w and h and page_area > 0 and (w * h) >= 0.5 * page_area:
            full_raster += 1

    required, advisory = _svg_labels(spec, scene_obj)
    candidates = [norm_ws(" ".join(text_nodes))]
    for el in root.iter():
        if _localname(el.tag) == "g":
            inner = norm_ws(" ".join("".join(t.itertext()) for t in el.iter() if _localname(t.tag) == "text"))
            if inner:
                candidates.append(inner)

    # per-panel text-index, for the token rule below
    panel_text = {}
    for el in root.iter():
        if _localname(el.tag) != "text":
            continue
        content = norm_ws("".join(el.itertext()))
        pid = el.get("data-panel")
        if pid:
            panel_text.setdefault(pid, []).append(content)
    for el in root.iter():
        if _localname(el.tag) != "g":
            continue
        pid = el.get("data-panel")
        if not pid:
            continue
        for t in el.iter():
            if _localname(t.tag) == "text":
                content = norm_ws("".join(t.itertext()))
                if content:
                    panel_text.setdefault(pid, []).append(content)
    panel_tokens = {pid: set(norm_ws(" ".join(lines)).split()) for pid, lines in panel_text.items()}
    for pid in panel_text:
        panel_tokens[pid] |= set(" ".join(panel_text[pid]).split())

    def _has(lab):
        lab = norm_ws(lab)
        return any(lab and lab in c for c in candidates)

    def _has_tokenwise(lab, pid):
        """Rule 2: every whitespace token of the label occurs in that panel's texts.

        This is how a title and its "(unit)" suffix drawn as two separate <text>
        nodes pass; it cannot hide a truncated/garbled label because every token
        must still be present, and the token match is reported separately.
        """
        toks = norm_ws(lab).split()
        if not toks or not pid:
            return False
        have = panel_tokens.get(pid)
        if not have:
            return False
        return all(t in have for t in toks)

    missing, tokenwise = [], []
    for lab, info in required.items():
        if _has(lab):
            continue
        if _has_tokenwise(lab, info["panel"]):
            tokenwise.append(lab)
        else:
            missing.append((info["origin"], lab))
    adv_missing = [(info["origin"], lab) for lab, info in advisory.items() if not _has(lab)]
    detail = ("svg_bytes=%d, text_nodes=%d, groups=%d(named=%d,unnamed=%d), ids=%d(unique=%d,dup=%d), "
              "images=%d(full_page=%d), required_labels=%d, labels_present=%d"
              "(contiguous=%d,token-covered=%d), labels_missing=%d, advisory_labels=%d(present=%d), "
              "rule={contiguous substring OR all label tokens present in that panel's text nodes}"
              % (len(raw.encode("utf-8")), len(text_nodes), len(groups), named, unnamed,
                 len(all_ids), len(set(all_ids)), len(dups), len(images), full_raster,
                 len(required), len(required) - len(missing),
                 len(required) - len(missing) - len(tokenwise), len(tokenwise), len(missing),
                 len(advisory), len(advisory) - len(adv_missing)))

    problems = []
    if not text_nodes:
        problems.append("0 non-empty <text> nodes (labels are paths, not editable text)")
    if missing:
        problems.append("%d required label(s) absent (neither contiguous nor token-covered), e.g. %r"
                        % (len(missing), missing[0][1][:40]))
    if unnamed:
        problems.append("%d unnamed <g> group(s)" % unnamed)
    if dups:
        problems.append("%d duplicate id(s): %s" % (len(dups), dups[:4]))
    if full_raster:
        problems.append("%d <image> element(s) cover >=50%% of the page" % full_raster)

    # round-trip: patch one real label in a COPY, assert it lands and structure survives
    roundtrip = "not run"
    if text_nodes and not problems:
        edit_mod = _soft_import("edit")
        if edit_mod is None:
            roundtrip = "edit module unavailable"
        else:
            tmp_dir = os.path.join(out_dir, ".edit_check")
            try:
                if os.path.isdir(tmp_dir):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                os.makedirs(tmp_dir)
                tmp_svg = os.path.join(tmp_dir, "figure.svg")
                with open(tmp_svg, "w", encoding="utf-8", newline="") as fh:
                    fh.write(raw)
                target = text_nodes[0]
                res = edit_mod.patch_svg_text(tmp_svg, {target: target + " [edited]"})
                ok = (res.get("changed_bytes") and res.get("applied") and res.get("parses")
                      and res.get("structure_preserved"))
                if not ok:
                    problems.append("patch_svg_text round-trip failed: applied=%s changed=%s parses=%s "
                                    "structure=%s" % (res.get("applied"), res.get("changed_bytes"),
                                                      res.get("parses"), res.get("structure_preserved")))
                roundtrip = ("patched 1 label(%r, %d chars), applied=%d changed=%s parses=%s "
                             "structure=%s ids_preserved=%s"
                             % (target[:16], len(target), len(res.get("applied") or []),
                                res.get("changed_bytes"), res.get("parses"),
                                res.get("structure_preserved"), res.get("ids_preserved")))
            except Exception as exc:
                problems.append("patch_svg_text raised %s: %s" % (type(exc).__name__, exc))
                roundtrip = "raised %s" % type(exc).__name__
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
    detail += ", roundtrip={%s}" % roundtrip
    if problems:
        return "fail", detail + " | " + "; ".join(problems)
    if not required:
        return "warn", detail + " | scene and spec declare 0 labels -> label presence is vacuous"
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate G — determinism
# --------------------------------------------------------------------------- #
def gate_g(spec, scene_obj, export_report, out_dir, materials):
    names = [n for n in ("figure.svg", "figure.png", "figure.pdf")
             if os.path.exists(os.path.join(out_dir, n))]
    if not names:
        return "fail", "no exported artefacts in %s (0 files)" % out_dir
    if not scene_obj:
        return "fail", "%d artefact(s) present but scene.json is empty -> re-export UNVERIFIABLE" % len(names)
    export_mod = _soft_import("export")
    if export_mod is None:
        return "fail", "export module unavailable; cannot re-export %d artefact(s)" % len(names)
    fs = resolve_fs(spec)
    formats = tuple(n.split(".")[-1] for n in names)
    tmp = os.path.join(out_dir, ".determinism_check")
    hashes, identical = {}, True
    try:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        export_mod.export_all(scene_obj, spec, fs, tmp, formats=formats)
        for n in names:
            h1 = export_mod.sha256_file(os.path.join(out_dir, n))
            p2 = os.path.join(tmp, n)
            h2 = export_mod.sha256_file(p2) if os.path.exists(p2) else None
            same = h1 == h2
            identical = identical and same
            hashes[n] = {"run1": h1[:16], "run2": (h2 or "MISSING")[:16], "identical": same}
    except Exception as exc:
        return "fail", ("re-export raised %s: %s (0/%d artefacts compared)"
                        % (type(exc).__name__, exc, len(names)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    claimed = ((export_report or {}).get("determinism") or {}).get("identical")
    detail = ("re-exported %d artefact(s) to %s; identical=%s; %s"
              % (len(names), ".determinism_check", identical,
                 "; ".join("%s run1=%s run2=%s" % (n, v["run1"], v["run2"]) for n, v in sorted(hashes.items()))))
    if claimed is not None:
        detail += "; export_report_claims_identical=%s" % claimed
    if not identical:
        bad = [n for n, v in hashes.items() if not v["identical"]]
        return "fail", detail + " | NON-DETERMINISTIC: %s" % bad
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate H — export quality
# --------------------------------------------------------------------------- #
def gate_h(spec, scene_obj, export_report, out_dir, materials):
    export_mod = _soft_import("export")
    if export_mod is None or not hasattr(export_mod, "sniff_png"):
        return "fail", "export module (byte sniffers) unavailable -> PNG/PDF quality UNVERIFIABLE"
    canvas = canvas_of(scene_obj, spec)
    rep = export_report or {}
    dpi = rep.get("dpi") or canvas.get("dpi") or 300
    try:
        dpi = int(round(float(dpi)))
    except Exception:
        dpi = 300
    page = rep.get("page_mm") or [canvas["w_mm"], canvas["h_mm"]]
    # CONTRACT §7 H: "page matches the request".  The request is the case's
    # figure.canvas; the spec's own meta.canvas is only a fallback.  Comparing
    # solely against the spec makes H self-consistent and blind to a pipeline
    # that ignores case.json (fixture A: case 120x90, spec+page 200x170 -> PASS).
    spec_canvas = dict(scene.DEFAULT_CANVAS)
    spec_canvas.update({k: float(v) for k, v in (((spec or {}).get("meta") or {}).get("canvas") or {}).items()
                        if _num(v)})
    spec_page = [spec_canvas["w_mm"], spec_canvas["h_mm"]]
    req_w, req_h, req_src = requested_canvas(materials, spec)
    if req_w is None:
        want_page, page_off = spec_page, None
    else:
        want_page = [req_w, req_h]
        page_off = max(abs(float(page[0]) - req_w), abs(float(page[1]) - req_h))
    spec_off = max(abs(spec_page[0] - want_page[0]), abs(spec_page[1] - want_page[1]))
    exp_px = [int(round(float(page[0]) / 25.4 * dpi)), int(round(float(page[1]) / 25.4 * dpi))]
    exp_box = [round(float(page[0]) * 72.0 / 25.4, 3), round(float(page[1]) * 72.0 / 25.4, 3)]

    problems, parts = [], []
    parts.append("page_mm=%s(requested %s by %s, off %.2f mm; spec.meta.canvas %s, off %.2f mm)"
                 % ([round(float(page[0]), 2), round(float(page[1]), 2)], want_page, req_src,
                    (page_off if page_off is not None else -1.0), spec_page, spec_off))
    if page_off is None:
        problems.append("case.json figure.canvas is unreadable (%s) -> page request UNVERIFIABLE" % req_src)
    elif page_off > 0.5:
        problems.append("emitted page %sx%s mm != requested %sx%s mm from %s (off %.2f mm)"
                        % (page[0], page[1], want_page[0], want_page[1], req_src, page_off))
    if req_w is not None and spec_off > 0.5:
        problems.append("spec.meta.canvas %sx%s mm != case request %sx%s mm (off %.2f mm): the pipeline "
                        "did not adopt figure.canvas"
                        % (spec_page[0], spec_page[1], want_page[0], want_page[1], spec_off))
    png_path = os.path.join(out_dir, "figure.png")
    if not os.path.exists(png_path):
        problems.append("figure.png missing")
        parts.append("png=missing")
    else:
        s = export_mod.sniff_png(png_path)
        mdpi = s.get("dpi")
        mdpi_eff = round(float(mdpi)) if mdpi is not None else None
        parts.append("png_px=%s(expected %s)" % (s.get("px"), exp_px))
        parts.append("png_dpi_meta=%s(need>=300)" % mdpi)
        parts.append("png_pHYs=%s" % s.get("has_phys"))
        if s.get("px") != exp_px:
            problems.append("PNG px %s != page/dpi expectation %s" % (s.get("px"), exp_px))
        if mdpi is None:
            problems.append("PNG has no resolution metadata (0 dpi recorded)")
        elif mdpi_eff < 300:
            problems.append("PNG dpi metadata %.4f (rounds to %d) < 300" % (float(mdpi), mdpi_eff))

    pdf_path = os.path.join(out_dir, "figure.pdf")
    if not os.path.exists(pdf_path):
        problems.append("figure.pdf missing")
        parts.append("pdf=missing")
    else:
        p = export_mod.sniff_pdf(pdf_path)
        mb = p.get("mediabox_pt")
        parts.append("pdf_pages=%s(need 1)" % p.get("pages"))
        parts.append("pdf_mediabox=%s(expected %s)pt" % (mb, exp_box))
        parts.append("pdf_font_programs=%s embedded=%s" % (p.get("font_programs"), p.get("fonts_embedded")))
        parts.append("pdf_text_ops=%s vector=%s" % (p.get("text_ops"), p.get("vector_text")))
        parts.append("pdf_image_objects=%s" % p.get("image_objects"))
        if p.get("pages") != 1:
            problems.append("PDF page count %s != 1" % p.get("pages"))
        if mb is None:
            problems.append("PDF has no MediaBox (0 measured)")
        elif abs(mb[0] - exp_box[0]) > 1.0 or abs(mb[1] - exp_box[1]) > 1.0:
            problems.append("PDF MediaBox %s != page %s pt" % (mb, exp_box))
        if not p.get("fonts_embedded"):
            problems.append("PDF embeds 0 font programs (no FontFile2/FontFile3)")
        if not p.get("vector_text"):
            problems.append("PDF content has 0 Tj/TJ text operators (text is not vector)")
    # cross-check every number export.py reported against what we just measured.
    # (The previous version compared rp["px"] to a re-cast of itself, which is
    # tautologically false, so a wholesale falsified report passed H, G and I.)
    rp = rep.get("png") or {}
    if rp.get("px") and os.path.exists(png_path):
        if [int(v) for v in rp["px"]] != list(s.get("px") or []):
            problems.append("export_report.png.px=%s != measured %s" % (rp.get("px"), s.get("px")))
    rd = rep.get("dpi")
    if rd is not None and abs(float(rd) - float(dpi)) > 1.0:
        problems.append("export_report.dpi=%s != scene dpi %s" % (rd, dpi))
    rpages = (rep.get("pdf") or {}).get("pages")
    if rpages is not None and os.path.exists(pdf_path) and rpages != p.get("pages"):
        problems.append("export_report.pdf.pages=%s != measured %s" % (rpages, p.get("pages")))
    rfe = (rep.get("pdf") or {}).get("fonts_embedded")
    if rfe is not None and os.path.exists(pdf_path) and bool(rfe) != bool(p.get("fonts_embedded")):
        problems.append("export_report.pdf.fonts_embedded=%s != measured %s"
                        % (rfe, p.get("fonts_embedded")))
    rvec = (rep.get("pdf") or {}).get("vector")
    if rvec is not None and os.path.exists(pdf_path) and bool(rvec) != bool(p.get("vector_text")):
        problems.append("export_report.pdf.vector=%s != measured %s" % (rvec, p.get("vector_text")))
    rpage = rep.get("page_mm")
    if rpage and (abs(float(rpage[0]) - float(page[0])) > 0.5
                  or abs(float(rpage[1]) - float(page[1])) > 0.5):
        problems.append("export_report.page_mm=%s != measured %s" % (rpage, page))
    parts.append("report_cross_checked=px,dpi,pages,fonts,vector,page_mm")
    detail = ", ".join(parts)
    if problems:
        return "fail", detail + " | " + "; ".join(problems)
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate I — contract
# --------------------------------------------------------------------------- #
def _nonempty(v):
    if v is None:
        return False
    if isinstance(v, (str, list, dict, tuple)):
        return len(v) > 0
    return True


def gate_i(spec, scene_obj, export_report, out_dir, materials):
    result_path = os.path.join(out_dir, "result.json")
    if not os.path.exists(result_path):
        return "fail", "result.json missing at %s (0 keys) -> contract UNVERIFIABLE" % result_path
    try:
        with open(result_path, "r", encoding="utf-8-sig") as fh:
            result = json.load(fh)
    except Exception as exc:
        return "fail", "result.json does not parse: %s" % exc
    case_id = result.get("case_id")
    contract = (materials or {}).get("output_contract")
    if not isinstance(contract, dict) or not contract:
        cj = (materials or {}).get("case_json_path")
        if cj and os.path.exists(cj):
            try:
                with open(cj, "r", encoding="utf-8-sig") as fh:
                    contract = (json.load(fh) or {}).get("output_contract")
            except Exception:
                contract = None
    keys = list(contract.keys()) if isinstance(contract, dict) else []
    hard_empty, soft_empty = [], []
    for k in keys:
        v = result.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            hard_empty.append(k)
        elif isinstance(v, (list, dict, tuple)) and len(v) == 0:
            soft_empty.append(k)
    unfilled = hard_empty + soft_empty
    artifacts = result.get("artifacts") or {}
    missing_files, sha_bad, bytes_bad = [], [], []
    self_written, checked = [], 0
    export_mod = _soft_import("export")
    for name, rec in (artifacts.items() if isinstance(artifacts, dict) else []):
        if not isinstance(rec, dict):
            missing_files.append(name)
            continue
        p = rec.get("path") or name
        full = p if os.path.isabs(str(p)) else os.path.join(out_dir, str(p))
        if not os.path.exists(full):
            missing_files.append("%s->%s" % (name, p))
            continue
        base = os.path.basename(full)
        if base == "validation_report.json":
            # this very check run rewrites validation_report.json, so the hash
            # recorded by an earlier result.json cannot be compared to it
            self_written.append(base)
            continue
        checked += 1
        if rec.get("bytes") is not None and int(rec["bytes"]) != os.path.getsize(full):
            bytes_bad.append(name)
        if rec.get("sha256") and export_mod is not None and hasattr(export_mod, "sha256_file"):
            if export_mod.sha256_file(full) != rec["sha256"]:
                sha_bad.append(name)
    detail = ("result_keys=%d, case_id=%s, contract_keys=%d, hard_empty=%d, soft_empty=%d, artifacts=%d, "
              "sha_checked=%d, self_written_skipped=%d%s, missing_files=%d, sha_mismatch=%d, bytes_mismatch=%d"
              % (len(result), repr(case_id)[:40], len(keys), len(hard_empty), len(soft_empty),
                 len(artifacts.keys()) if isinstance(artifacts, dict) else 0,
                 checked, len(self_written),
                 ("(" + ",".join(self_written) + ")") if self_written else "",
                 len(missing_files), len(sha_bad), len(bytes_bad)))
    problems = []
    if not _nonempty(case_id):
        problems.append("case_id is empty")
    if hard_empty:
        problems.append("%d contract key(s) empty/missing: %s" % (len(hard_empty), hard_empty[:6]))
    if soft_empty:
        detail += " | declared-empty list/dict keys (not a pass): %s" % soft_empty[:6]
    if missing_files:
        problems.append("%d artifact path(s) do not exist: %s" % (len(missing_files), missing_files[:4]))
    if sha_bad:
        problems.append("%d artifact sha256 mismatch: %s" % (len(sha_bad), sha_bad[:4]))
    if bytes_bad:
        problems.append("%d artifact byte count mismatch: %s" % (len(bytes_bad), bytes_bad[:4]))
    if problems:
        return "fail", detail + " | " + "; ".join(problems)
    if not keys:
        return "warn", detail + " | 0 output_contract keys available -> contract coverage UNVERIFIABLE"
    if soft_empty:
        return "warn", detail
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate J — mark <-> source-data two-way mapping (CONTRACT §4.1, E910)
# --------------------------------------------------------------------------- #
MARK_KINDS = ("pt", "bar", "cell", "eb", "box", "sector", "hm", "sig")
_NA_TOKENS = ("", "na", "n/a", "nan", "null", "none", "-", "--", "nd")
_N_COL_NAMES = ("n", "sample_size", "samplesize", "replicates", "replicate", "rep",
                "n_obs", "nobs", "n_ind")
_NUM_RE = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def _as_number(v):
    """Parse a material cell as a float, tolerating thousands separators / signs."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("\u00a0", "").replace("\u2009", "")
    if s.lower() in _NA_TOKENS:
        return None
    s2 = s.replace(",", "")
    if "," in s and _NUM_RE.match(s):
        s2 = s
    return float(s2) if _NUM_RE.match(s2) else None


def _norm_path(p):
    return str(p or "").replace("\\", "/").lstrip("./")


def _same_file(a, b):
    na, nb = _norm_path(a), _norm_path(b)
    return na == nb or na.endswith("/" + nb) or nb.endswith("/" + na) \
        or os.path.basename(na) == os.path.basename(nb)


def _load_json_file(path):
    if not path or not os.path.exists(path):
        return None, "missing"
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh), "ok"
    except Exception as exc:
        return None, "unreadable (%s: %s)" % (type(exc).__name__, exc)


def _material_tables(materials):
    """Flatten every parsed table: [{file, mat, table}]."""
    out = []
    mats = (materials or {}).get("materials")
    if not isinstance(mats, list):
        mats = materials if isinstance(materials, list) else []
    for m in mats:
        if not isinstance(m, dict):
            continue
        path = str(m.get("path") or "")
        for t in (m.get("tables") or []):
            if isinstance(t, dict):
                out.append({"file": path, "mat": m, "table": t})
    return out


def _parse_cell_key(cell):
    """``file#table:row:col`` -> (file, table, row:int, col) or (None,)*4."""
    s = str(cell or "").strip()
    if "#" not in s or ":" not in s:
        return None, None, None, None
    f, rest = s.split("#", 1)
    parts = rest.split(":")
    if len(parts) < 3 or not f:
        return None, None, None, None
    try:
        row = int(parts[1])
    except (TypeError, ValueError):
        return None, None, None, None
    return f, parts[0], row, ":".join(parts[2:])


def _resolve_cell(tables, file, table, row, col, want=None):
    """Locate ``file#table:row:col`` in the parsed materials.

    Returns ``(text_value, how, locator)`` or ``(None, reason, None)``.  Both the
    physical-row (header = row 1 -> data starts at row 2) and the data-index
    (first data row = 1) conventions are accepted: the frozen schema does not pin
    one down.  When ``want`` is numeric the interpretation whose cell value
    matches is preferred; otherwise the first is returned so the mismatch is
    reported against a real cell rather than a guessed one.
    """
    if not tables:
        return None, "no parsed tables in materials", None
    cands = [t for t in tables if _same_file(t["file"], file)]
    if not cands:
        return None, "file %r not found among %d material table(s)" % (file, len(tables)), None
    tname = _norm_path(table)
    pick = None
    for t in cands:
        nm = str(t["table"].get("name") or "")
        if _norm_path(nm) == tname or os.path.basename(_norm_path(nm)) == os.path.basename(tname):
            pick = t
            break
    if pick is None and len(cands) == 1:
        pick = cands[0]
    if pick is None:
        return None, "table %r not in %s (have %s)" % (table, file,
                                                       [t["table"].get("name") for t in cands]), None
    tt = pick["table"]
    hdr = [str(h) for h in (tt.get("header") or [])]
    rows = tt.get("rows") or []
    ci = None
    for i, h in enumerate(hdr):
        if h == col:
            ci = i
            break
    if ci is None:
        for i, h in enumerate(hdr):
            if h.strip().lower() == str(col).strip().lower():
                ci = i
                break
    if ci is None and str(col).strip().isdigit():
        j = int(str(col).strip()) - 1
        if 0 <= j < len(hdr):
            ci = j
    if ci is None:
        return None, "column %r not in header %s" % (col, hdr), None
    tries = []
    if 1 <= row <= len(rows):
        tries.append(("data-index row %d" % row, rows[row - 1]))
    if 2 <= row <= len(rows) + 1:
        tries.append(("physical row %d (header=1)" % row, rows[row - 2]))
    if not tries:
        return None, "row %d out of range (table has %d data row(s))" % (row, len(rows)), None
    out = []
    for how, r in tries:
        cells = r.get("cells") or []
        if ci < len(cells):
            out.append((str(cells[ci]), how, str(r.get("locator") or "row ?")))
    if not out:
        return None, "row %d has no cell at column index %d" % (row, ci), None
    if want is not None:
        for c in out:
            g = _as_number(c[0])
            if g is not None and abs(g - float(want)) <= 1e-9:
                return c
    return out[0]


def _label_verbatim(label, mat_texts):
    """(bool, matched_file) — is ``label`` a verbatim (whitespace-normalised) span?"""
    want = norm_ws(label)
    if not want:
        return True, None
    for m in mat_texts:
        if want in norm_ws(m["text"]):
            return True, m["path"]
    return False, None


def _scene_mark_ids(scene_obj):
    """Mark ids the renderer says it drew (layout-owned; may be absent)."""
    out = set()
    for p in (scene_obj or {}).get("primitives") or []:
        if not isinstance(p, dict):
            continue
        for key in ("mark_id", "mark_ids", "marks"):
            v = p.get(key)
            if isinstance(v, str) and v:
                out.add(v)
            elif isinstance(v, (list, tuple)):
                for it in v:
                    if isinstance(it, str):
                        out.add(it)
                    elif isinstance(it, dict) and it.get("mark_id"):
                        out.add(str(it["mark_id"]))
    return out


def gate_j(spec, scene_obj, export_report, out_dir, materials):
    """图元 <-> 源数据双向映射 must be complete and honest (E910)."""
    charts = iter_all_charts(spec)
    expected_vals = sum(len(s.get("values") or [])
                        for _p, c in charts for s in (c.get("series") or []) if isinstance(s, dict))
    mdm, why = _load_json_file(os.path.join(out_dir, "mark_data_map.json"))
    if mdm is None:
        return "fail", ("E910 map_missing: mark_data_map.json %s in %s; %d chart(s), %d plotted value(s) "
                        "require a two-way map -> mark<->source provenance UNVERIFIABLE, status must be partial"
                        % (why, out_dir, len(charts), expected_vals))
    if not isinstance(mdm, dict):
        return "fail", "E910 map_not_object: mark_data_map.json top level is %s (need object)" % type(mdm).__name__
    schema = mdm.get("schema_version")
    marks = mdm.get("marks")
    cells = mdm.get("cells")
    unmapped = mdm.get("unmapped") or []
    ambiguous = mdm.get("ambiguous") or []
    if not isinstance(marks, list) or not isinstance(cells, dict):
        return "fail", ("E910 map_shape: marks=%s (need list) and cells=%s (need object); schema_version=%r"
                        % (type(marks).__name__, type(cells).__name__, schema))

    tables = _material_tables(materials)
    mat_texts = _material_texts(materials)
    have_mats = any(m["text"] for m in mat_texts)
    scene_marks = _scene_mark_ids(scene_obj)

    bad_source, bad_cell, bad_value, bad_label, bad_rev, bad_rrev = [], [], [], [], [], []
    dup_ids, derived_nosrc = [], []
    seen, our_cells = set(), {}
    n_numeric = n_label_checked = n_objsrc = 0
    for i, mk in enumerate(marks):
        if not isinstance(mk, dict):
            bad_source.append("marks[%d] is %s, not an object" % (i, type(mk).__name__))
            continue
        mid = mk.get("mark_id")
        if not isinstance(mid, str) or not mid:
            bad_source.append("marks[%d] has no mark_id" % i)
            continue
        if mid in seen:
            dup_ids.append(mid)
        seen.add(mid)
        src = mk.get("source")
        if not isinstance(src, dict) or not src:
            bad_source.append("%s: no source object (numeric marks may not be sourceless)" % mid)
            continue
        n_objsrc += 1
        cell = src.get("cell")
        f, tbl, row, col = _parse_cell_key(cell) if cell else (None, None, None, None)
        if f is None:
            bad_cell.append("%s: source.cell %r is not 'file#table:row:col'" % (mid, cell))
            continue
        our_cells.setdefault(cell, []).append(mid)
        text_val, how, locator = _resolve_cell(tables, f, tbl, row, col,
                                               want=(mk.get("value") if _num(mk.get("value")) else None))
        if text_val is None:
            bad_cell.append("%s: cell %s unresolvable (%s)" % (mid, cell, how))
            continue
        v = mk.get("value")
        if _num(v):
            n_numeric += 1
            got = _as_number(text_val)
            if got is None:
                bad_value.append("%s: cell %s holds %r (not numeric) while value=%r" % (mid, cell, text_val, v))
            elif abs(got - float(v)) > 1e-9:
                bad_value.append("%s: value=%r != cell %s (%s) value=%r |diff|=%.3g"
                                 % (mid, v, cell, locator, got, abs(got - float(v))))
        elif mk.get("derived"):
            if not (mk.get("source_values") or []):
                derived_nosrc.append(mid)
        # categorical marks: the label must appear verbatim in a material
        lab = mk.get("category") or src.get("row_label")
        if isinstance(lab, str) and lab.strip():
            n_label_checked += 1
            ok, _f = _label_verbatim(lab, mat_texts)
            if not ok:
                bad_label.append("%s: label %r not verbatim in any material" % (mid, lab))

    # reverse index: every mark's cell must be listed under cells and point back
    for cell, mids in our_cells.items():
        declared = cells.get(cell)
        if not isinstance(declared, list):
            bad_rev.append("%s: cells[%r] missing/not a list" % (mids[0], cell))
            continue
        miss = [m for m in mids if m not in declared]
        if miss:
            bad_rev.append("%s: cells[%r] lacks %s" % (mids[0], cell, miss))
    for ck, cv in cells.items():
        if not isinstance(cv, list):
            bad_rrev.append("cells[%r] is %s, not a list" % (ck, type(cv).__name__))
            continue
        for m in cv:
            if m not in seen:
                bad_rrev.append("cells[%r] -> unknown mark %r" % (ck, m))
    cov_missing = sorted(m for m in scene_marks if m not in seen)

    detail = ("map=mark_data_map.json(%s), schema_version=%r, marks=%d, numeric_marks=%d, marks_with_source=%d, "
              "cells_index=%d, materials=%d(text=%s), tables=%d, plotted_values_expected=%d, "
              "scene_declared_marks=%d, label_checked=%d, source_missing=%d, cell_unresolved=%d, "
              "value_mismatch=%d, label_not_verbatim=%d, derived_without_source_values=%d, "
              "reverse_index_missing=%d, reverse_unknown=%d, dup_ids=%d, coverage_missing=%d, "
              "unmapped=%d, ambiguous=%d"
              % (why, schema, len(marks), n_numeric, n_objsrc, len(cells), len(mat_texts), have_mats,
                 len(tables), expected_vals, len(scene_marks), n_label_checked, len(bad_source),
                 len(bad_cell), len(bad_value), len(bad_label), len(derived_nosrc), len(bad_rev),
                 len(bad_rrev), len(dup_ids), len(cov_missing), len(unmapped), len(ambiguous)))

    if not have_mats:
        return "fail", detail + (" | UNVERIFIABLE: no material text, 0 of %d marks can be checked against "
                                 "source data; status must be partial" % len(marks))
    if not marks and expected_vals > 0:
        return "fail", detail + " | E910 map_empty: 0 marks for %d plotted value(s)" % expected_vals
    if unmapped:
        return "fail", detail + " | E910 unmapped=%s" % (list(unmapped)[:5],)
    if ambiguous:
        return "fail", detail + " | E910 ambiguous=%s" % (list(ambiguous)[:5],)
    if dup_ids:
        return "fail", detail + " | E910 duplicate mark_id(s): %s" % dup_ids[:5]
    if bad_source:
        return "fail", detail + " | E910 %d mark(s) without a usable source; first: %s" % (len(bad_source), bad_source[0])
    if bad_cell:
        return "fail", detail + " | E910 %d cell locator(s) unresolvable; first: %s" % (len(bad_cell), bad_cell[0])
    if bad_value:
        return "fail", detail + " | E910 %d value mismatch(es) > 1e-9; first: %s" % (len(bad_value), bad_value[0])
    if bad_label:
        return "fail", detail + " | E910 %d label(s) not verbatim in material; first: %s" % (len(bad_label), bad_label[0])
    if derived_nosrc:
        return "fail", detail + (" | E910 derived mark(s) without source_values: %s" % derived_nosrc[:5])
    if bad_rev:
        return "fail", detail + " | E910 reverse index incomplete; first: %s" % bad_rev[0]
    if bad_rrev:
        return "fail", detail + " | E910 reverse index points at unknown marks; first: %s" % bad_rrev[0]
    if cov_missing:
        return "fail", detail + (" | E910 %d drawn mark(s) absent from marks[]; first: %s"
                                 % (len(cov_missing), cov_missing[0]))
    if expected_vals and len(marks) < expected_vals:
        return "warn", detail + (" | W910 marks=%d < plotted values=%d: coverage shortfall "
                                 "(scene published no mark ids, so this is a lower bound)"
                                 % (len(marks), expected_vals))
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate K — annotation completeness (CONTRACT §6/§7, E911–E915 / W920 / W922)
# --------------------------------------------------------------------------- #
ERROR_KINDS = ("sd", "sem", "ci95", "iqr", "range")
QUANT_CHART_TYPES = ("bar", "barh", "line", "scatter", "dot", "heatmap", "box", "violin", "waterfall")
BAR_LIKE_TYPES = ("bar", "barh", "waterfall")
_N_DECL_RE = re.compile(r"(?:^|[^A-Za-z])n\s*[=:]\s*\d+", re.I)
_N_WORD_RE = re.compile(r"(?:sample\s*size|number\s+of\s+(?:animals|samples|replicates)|\bn\s*=\s*\d+)", re.I)


def _chart_series(chart):
    return [s for s in (chart.get("series") or []) if isinstance(s, dict)]


def _chart_number_sets(chart):
    vals = []
    for s in _chart_series(chart):
        vals += [v for v in (s.get("values") or []) if _num(v)]
    xd = chart.get("x")
    if isinstance(xd, dict):
        vals += [v for v in (xd.get("values") or []) if _num(v)]
    elif isinstance(xd, list):
        vals += [v for v in xd if _num(v)]
    return vals


def _table_has_n_column(t):
    hdr = [str(h).strip().lower() for h in (t.get("header") or [])]
    return any(h in _N_COL_NAMES for h in hdr)


def _repeated_groups(t):
    """Row groups sharing every non-numeric column -> genuine replicate rows."""
    hdr = t.get("header") or []
    rows = t.get("rows") or []
    if not hdr or len(rows) < 2:
        return 0, []
    numeric = []
    for ci in range(len(hdr)):
        vals = [(r.get("cells") or [None] * len(hdr))[ci] if ci < len(r.get("cells") or []) else None
                for r in rows]
        nonempty = [v for v in vals if str(v).strip().lower() not in _NA_TOKENS]
        numeric.append(bool(nonempty) and all(_as_number(v) is not None for v in nonempty))
    key_cols = [ci for ci in range(len(hdr))
                if not numeric[ci] and str(hdr[ci]).strip().lower() not in _N_COL_NAMES]
    if not key_cols:
        return 0, []
    groups = {}
    for r in rows:
        cells = list(r.get("cells") or [])
        if len(cells) < len(hdr):
            cells += [""] * (len(hdr) - len(cells))
        key = tuple(str(cells[ci]) for ci in key_cols)
        groups[key] = groups.get(key, 0) + 1
    reps = sorted((k, v) for k, v in groups.items() if v > 1)
    return len(reps), reps[:3]


def _caption_text(spec):
    texts = (spec or {}).get("texts") or {}
    meta = (spec or {}).get("meta") or {}
    for v in (texts.get("caption"), texts.get("legend_note"), meta.get("caption")):
        if isinstance(v, str) and v.strip():
            return v
    return ""


def gate_k(spec, scene_obj, export_report, out_dir, materials):
    """Annotation completeness: axes+units, zero baseline, errbar kind, sig, n (E911–E915)."""
    charts = iter_all_charts(spec)
    caption = _caption_text(spec)
    cap_declares_n = bool(_N_WORD_RE.search(caption)) or bool(_N_DECL_RE.search(caption))
    fig, stale_caption, case_how = case_figure(materials)
    tables = _material_tables(materials)
    rep_tables = [t for t in tables if not _table_has_n_column(t["table"])]
    rep_hits = [(t["file"], t["table"].get("name"), _repeated_groups(t["table"])) for t in rep_tables]
    rep_hits = [(f, n, gg) for f, n, gg in rep_hits if gg[0] > 0]
    n_col_tables = [t for t in tables if _table_has_n_column(t["table"])]

    problems, warns, parts = [], [], []
    n_series = n_errbar = n_sig = n_baseline = n_unit = n_axiswarn = 0
    n_charts = len(charts)
    for _panel, chart in charts:
        cp = chart_path(spec, chart)
        ctype = str(chart.get("type") or "")
        series = _chart_series(chart)
        axis = chart.get("axis") if isinstance(chart.get("axis"), dict) else {}
        yax = axis.get("y") if isinstance(axis.get("y"), dict) else {}
        xax = axis.get("x") if isinstance(axis.get("x"), dict) else {}
        # --- E914: quantitative axes need units -----------------------------
        if ctype in QUANT_CHART_TYPES:
            y_unit = chart.get("unit") or yax.get("unit")
            if not (isinstance(y_unit, str) and y_unit.strip()):
                n_unit += 1
                problems.append("E914 %s.unit: %s chart without a y-axis unit" % (cp, ctype))
            xd = chart.get("x")
            x_numeric = (isinstance(xd, dict) and any(_num(v) for v in (xd.get("values") or []))) \
                or (isinstance(xd, list) and any(_num(v) for v in xd))
            x_unit = chart.get("x_unit") or xax.get("unit")
            if ctype in ("line", "scatter", "dot", "box") and x_numeric \
                    and not (isinstance(x_unit, str) and x_unit.strip()):
                n_unit += 1
                problems.append("E914 %s.x_unit: %s chart with numeric x and no x unit" % (cp, ctype))
        # --- E911: error bars must declare their meaning ---------------------
        for si, s in enumerate(series):
            errs = s.get("errors")
            kind = s.get("error_kind")
            if isinstance(errs, list) and errs:
                n_errbar += 1
                if not (isinstance(kind, str) and kind.strip()):
                    problems.append("E911 %s.series[%d].error_kind: %d error value(s) with no error_kind "
                                    "(series %r)" % (cp, si, len(errs), s.get("name")))
                elif kind.strip().lower() not in ERROR_KINDS:
                    problems.append("E911 %s.series[%d].error_kind=%r not in %s"
                                    % (cp, si, kind, list(ERROR_KINDS)))
                vals = s.get("values") or []
                if len(errs) != len(vals):
                    problems.append("E911 %s.series[%d]: errors=%d != values=%d"
                                    % (cp, si, len(errs), len(vals)))
            elif isinstance(kind, str) and kind.strip() and not errs:
                warns.append("W911 %s.series[%d]: error_kind=%r declared but errors[] empty" % (cp, si, kind))
            if s.get("n") is not None:
                n_series += 1
        # --- E912: significance must be defined, not invented ----------------
        for gi, sig in enumerate(chart.get("significance") or []):
            if not isinstance(sig, dict):
                problems.append("E912 %s.significance[%d] is not an object" % (cp, gi))
                continue
            n_sig += 1
            has_test = isinstance(sig.get("test"), str) and sig.get("test").strip()
            has_p = _num(sig.get("p"))
            has_def = bool(sig.get("defined_in_caption")) or bool(sig.get("defined"))
            if not has_test:
                problems.append("E912 %s.significance[%d] (%s): no test declared"
                                % (cp, gi, sig.get("between")))
            elif not (has_p or has_def):
                problems.append("E912 %s.significance[%d] (%s): neither p nor defined_in_caption "
                                "(a comparison the source does not quantify may not invent a p-value)"
                                % (cp, gi, sig.get("between")))
        # --- E915: bar-like charts must not truncate the baseline ------------
        if ctype in BAR_LIKE_TYPES:
            bl = chart.get("baseline")
            n_baseline += 1
            if _num(bl) and abs(float(bl)) > 1e-12 and not chart.get("justification"):
                problems.append("E915 %s.baseline=%r != 0 without justification" % (cp, bl))
        # --- W920: explicit tick range must cover the data -------------------
        for aname, ax in (("y", yax), ("x", xax)):
            ts, te = ax.get("tick_start"), ax.get("tick_end")
            if _num(ts) and _num(te):
                vals = [v for v in _chart_number_sets(chart) if _num(v)]
                if aname == "x":
                    xd = chart.get("x")
                    vx = (xd.get("values") if isinstance(xd, dict) else xd) or []
                    vals = [v for v in vx if _num(v)] or vals
                if vals and (min(vals) < float(ts) - 1e-9 or max(vals) > float(te) + 1e-9):
                    n_axiswarn += 1
                    warns.append("W920 %s.axis.%s ticks %s..%s exclude data %g..%g"
                                 % (cp, aname, ts, te, min(vals), max(vals)))
        # --- E913: repeated measurements must be reported --------------------
        chart_has_n = any(s.get("n") is not None for s in series)
        if rep_hits and not chart_has_n and not cap_declares_n:
            f, tn, (cnt, sample) = rep_hits[0]
            problems.append("E913 %s: material %s#%s has %d repeated group(s) (e.g. %s) but no series "
                            "carries n and the caption declares none" % (cp, f, tn, cnt, sample))
        elif rep_hits and not chart_has_n and cap_declares_n:
            warns.append("W913 %s: repeated groups in %s but only the caption declares n" % (cp, rep_hits[0][1]))
    # --- W922: stale caption -------------------------------------------------
    caption_used = norm_ws(caption)
    if stale_caption:
        if caption_used and caption_used == norm_ws(stale_caption):
            warns.append("W922 caption equals the case's stale_caption verbatim")
        else:
            units = set()
            for _p, chart in charts:
                u = chart.get("unit")
                if isinstance(u, str) and u.strip():
                    units.add(u.strip().lower())
            stale_units = set(re.findall(r"\(([A-Za-z%/µ]+)\)", stale_caption))
            clash = sorted(u for u in (s.lower() for s in stale_units) if units and u not in units)
            if clash:
                warns.append("W922 caption/system unit clash: stale_caption names %s, chart units are %s"
                             % (clash, sorted(units)))
    sa = (spec.get("audit") or {}) if isinstance(spec.get("audit"), dict) else {}
    detail = ("charts=%d, series=%d, errbar_series=%d, significance_entries=%d, bar_like=%d, unit_violations=%d, "
              "axis_range_warns=%d, repeated_tables=%d, n_column_tables=%d, series_with_n=%d, "
              "caption_declares_n=%s, stale_caption=%s, warnings=%d, violations=%d"
              % (n_charts, sum(len(_chart_series(c)) for _p, c in charts), n_errbar, n_sig, n_baseline,
                 n_unit, n_axiswarn, len(rep_hits), len(n_col_tables), n_series, cap_declares_n,
                 bool(stale_caption), len(warns), len(problems)))
    _ = sa
    if problems:
        return "fail", detail + " | " + "; ".join(problems[:6])
    if warns:
        return "warn", detail + " | " + "; ".join(warns[:6])
    return "pass", detail


# --------------------------------------------------------------------------- #
# gate L — typesetting / i18n (CONTRACT §7 L, E917)
# --------------------------------------------------------------------------- #
CJK_RE = re.compile(r"[\u2e80-\u2eff\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
                    r"\uf900-\ufaff\uff00-\uffef]")


def gate_l(spec, scene_obj, export_report, out_dir, materials):
    """CJK glyph coverage (no tofu) + long-label wrapping (E917)."""
    fs = resolve_fs(spec)
    prims = _text_prims(scene_obj)
    entries = []
    for p in prims:
        t = text_of(p)
        if t:
            entries.append((p, t))
    spec_text_bits = []
    for src in ((spec or {}).get("texts") or {},):
        if isinstance(src, dict):
            spec_text_bits += [v for v in src.values() if isinstance(v, str)]
    meta = (spec or {}).get("meta") or {}
    spec_text_bits += [meta.get("title"), meta.get("caption")]
    corpus = "\n".join([t for _p, t in entries] + [s for s in spec_text_bits if isinstance(s, str)])
    cjk_chars = sorted(set(CJK_RE.findall(corpus)))
    cjk_nodes = sum(1 for _p, t in entries if CJK_RE.search(t))

    cov = None
    mod = _soft_import("fonts")
    if mod is not None and hasattr(mod, "coverage_report") and fs is not None:
        try:
            cov = mod.coverage_report(corpus, fs)
        except Exception:
            cov = None
    faces = sorted(os.path.basename(p) for p in (fs.all_paths() if fs is not None else []))
    degraded = bool(((export_report or {}).get("font") or {}).get("degraded")) or bool(meta.get("degraded_fonts"))

    overflow = []
    n_measured = 0
    for p, t in entries:
        sp = size_pt_of(p)
        if sp is None:
            continue
        lines = p.get("lines") if isinstance(p.get("lines"), list) else None
        src_text = max((str(ln) for ln in lines), key=len) if lines else t
        mw, _src = measure_width_mm(src_text, sp, fs, bool(p.get("bold")))
        n_measured += 1
        container, ckey = None, "none"
        for k in _CONTAINER_KEYS:
            if _num(p.get(k)):
                container, ckey = float(p[k]), k
                break
        if container is None and isinstance(p.get("container"), dict) and _num(p["container"].get("w")):
            container, ckey = float(p["container"]["w"]), "container.w"
        if container is None:
            b = bbox_of(p)
            if b is not None:
                container, ckey = b["w"], "bbox.w"
        if container is not None and mw - container > 0.05:
            overflow.append((str(p.get("id", "?")), round(mw, 2), round(container, 2), ckey))

    detail = ("cjk_chars=%d(unique), cjk_text_nodes=%d, text_nodes=%d, measured=%d, font_faces=%d%s, "
              "coverage=%s, missing_glyphs=%s, degraded_declared=%s, long_label_overflow=%d"
              % (len(cjk_chars), cjk_nodes, len(prims), n_measured, len(faces),
                 ("(" + ",".join(faces[:3]) + ")") if faces else "", 
                 ("ok" if (cov or {}).get("ok") else ("unavailable" if cov is None else "fail")),
                 ((cov or {}).get("missing") or "")[:24] or "-", degraded, len(overflow)))
    if cjk_chars and cov is None:
        return "fail", detail + (" | E917 UNVERIFIABLE: %d unique CJK char(s) in the figure but glyph coverage "
                                 "could not be measured (no font face / no coverage API) -> status must be partial"
                                 % len(cjk_chars))
    if cov is not None and not cov.get("ok"):
        return "fail", detail + (" | E917 %d character(s) missing from every resolved face %s: %r"
                                 % (cov.get("missing_count", 0), faces, cov.get("missing")))
    if overflow:
        o = max(overflow, key=lambda x: x[1] - x[2])
        return "fail", detail + (" | long label %s measures %.2fmm > %.2fmm (%s), +%.2fmm: would be "
                                 "clipped/truncated" % (o[0], o[1], o[2], o[3], o[1] - o[2]))
    return "pass", detail


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
GATES = [
    ("A", "canvas/margins", gate_a),
    ("B", "typography", gate_b),
    ("C", "color-accessibility", gate_c),
    ("D", "encoding-fidelity", gate_d),
    ("E", "provenance", gate_e),
    ("F", "editability", gate_f),
    ("G", "determinism", gate_g),
    ("H", "export-quality", gate_h),
    ("I", "contract", gate_i),
    ("J", "mark-data-map", gate_j),
    ("K", "annotation-completeness", gate_k),
    ("L", "typesetting-i18n", gate_l),
]


def run_checks(spec, scene_obj, export_report, out_dir, materials):
    """Run gates A–L.  Returns {"checks": [...], "failed": [...], "warned": [...]}."""
    out_dir = os.path.abspath(out_dir)
    checks, failed, warned = [], [], []
    for gid, gname, fn in GATES:
        try:
            status, detail = fn(spec, scene_obj, export_report, out_dir, materials)
        except Exception as exc:  # a crashing gate is a failure, never a pass
            status, detail = "fail", "check raised %s: %s" % (type(exc).__name__, exc)
        if status not in ("pass", "fail", "warn"):
            status = "fail"
            detail = "invalid status from checker; " + str(detail)
        checks.append({"id": gid, "name": gname, "status": status, "detail": str(detail)})
        if status == "fail":
            failed.append(gid)
        elif status == "warn":
            warned.append(gid)
    return {"checks": checks, "failed": failed, "warned": warned}

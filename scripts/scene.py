#!/usr/bin/env python3
"""scene.py — frozen data model for the source-bound data-chart pipeline.

Single source of truth for: coordinate units, spec validation, deterministic ids.
No third-party imports. Safe on a bare CPython 3.8+.

COORDINATES (contract §2, do not change):
    * scene space is MILLIMETRES, origin at the TOP-LEFT corner, +y points DOWN.
    * font sizes are POINTS (1 pt = 25.4/72 mm).
    * a "bbox" is always {"x","y","w","h"} in mm, x/y = top-left corner.

Spec model (CONTRACT §6): charts live in ``panels[].charts[]`` (no top-level
``charts``); each chart carries ``type``/``unit``/``x_unit``/``categories``/
``baseline``/``axis``/``significance`` and each series carries ``values``/``x``/
``errors``/``error_kind``/``n``.  The structural checks for those fields live in
this module and emit the frozen CONTRACT §7 codes:

    E911 errors without a declared error_kind / length mismatch
    E912 significance without between/label, or undefined test+p
    E913 sample size malformed
    E914 axis malformed, bad scale, or a quantitative axis with no unit
    E915 bar-like chart with a non-zero baseline and no justification

E911/E912/E913/E914-unit/E915-drawing are reported as *warnings* here on purpose:
a figure with an undeclared error-bar meaning must still be RENDERED, so that
gate K can fail it with a real artefact and a `partial` status instead of the
build dying with exit 4.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import hashlib
import json
import os
import re

VERSION = "1.0"

MM_PER_IN = 25.4
PT_MM = 25.4 / 72.0          # mm per point
MM_PT = 72.0 / 25.4          # points per mm

SEVERITY_ORDER = {"error": 0, "warn": 1}

# Minimum typography, in points (contract §2 / §6 B).
MIN_BODY_PT = 7.0
MIN_PANEL_TITLE_PT = 8.0
MIN_MAIN_TITLE_PT = 11.0

DEFAULT_CANVAS = {"w_mm": 180.0, "h_mm": 120.0, "margin_mm": 3.0, "dpi": 300}

SHAPES = {"rrect", "rect", "circle", "ellipse", "hexagon", "diamond", "capsule", "note"}
PANEL_ROLES = ("problem", "method", "results", "conclusion")
EDGE_KINDS = ("activation", "inhibition", "flow", "spatial", "feedback", "translocation")
CHART_TYPES = {"bar", "barh", "line", "scatter", "dot", "heatmap", "box", "violin",
               "waterfall", "none"}

# CONTRACT §6: error-bar meaning, axis scales, and which chart types are
# "bar-like" (they must sit on a zero baseline, CONTRACT §7 E915).
ERROR_KINDS = ("sd", "sem", "ci95", "iqr", "range")
AXIS_SCALES = ("linear", "log10")
BAR_LIKE_TYPES = ("bar", "barh", "waterfall")
QUANT_AXIS_TYPES = ("bar", "barh", "line", "scatter", "dot", "box", "violin",
                    "waterfall")
# chart types that consume a distribution of samples rather than one value per
# category (their statistics are computed from the real values, never estimated)
DISTRIBUTION_TYPES = ("box", "violin")

# The one layout mode that still demands the four diagrammatic roles
# (problem -> method -> results -> conclusion).  A data chart must not be
# penalised for lacking them (chart-engine task item 1).
GRAPHICAL_ABSTRACT_MODE = "graphical_abstract"

# Okabe-Ito: the canonical colour-vision-deficiency safe categorical palette.
#
# ORDER MATTERS.  The set is CVD-safe, but three of its members are nearly
# iso-luminant (D55E00 L*=54.2, 009E73 L*=57.7, CC79A7 L*=61.0), so a greyscale
# print cannot tell them apart (measured worst pair dL* = 3.5, below the 5.0
# floor gate C enforces).  The order below is chosen so that the first four
# colours a figure actually uses are separated in L* as well as in hue:
#   0072B2 L*=46.0 | E69F00 L*=70.6 | 009E73 L*=57.7 | 000000 L*=0.0
#   -> worst pairwise dL* = 11.7, worst CVD dE well above the 6.0 floor.
# Beyond four categories the greyscale floor cannot be met from this set alone;
# gate C will say so with measured numbers rather than passing silently.
PALETTES = {
    "okabe-ito": [
        "#0072B2", "#E69F00", "#009E73", "#000000",
        "#F0E442", "#D55E00", "#CC79A7", "#56B4E9",
    ],
    "grayscale-safe": ["#1a1a1a", "#4d4d4d", "#808080", "#b3b3b3"],
}


def categorical_colors(k):
    """The first ``k`` colours of the CVD- and greyscale-aware categorical order."""
    pal = PALETTES["okabe-ito"]
    return [pal[i % len(pal)] for i in range(max(0, int(k)))]

# Sequential cmaps (hex stops) used for colorbars / heatmaps.
CMAPS = {
    "viridis": ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
    "batlow": ["#011959", "#10406a", "#1c6f6a", "#8f9e43", "#f7e225"],
    "cividis": ["#00224e", "#335c67", "#7b8b6f", "#c7b86f", "#fee838"],
    "grey": ["#f7f7f7", "#000000"],
}


class SpecError(ValueError):
    """Raised when a spec cannot be used at all (CLI exit 4)."""

    def __init__(self, message, issues=None):
        super().__init__(message)
        self.issues = issues or []


# --------------------------------------------------------------------------- #
# units
# --------------------------------------------------------------------------- #
def mm_to_pt(v):
    return float(v) * MM_PT


def pt_to_mm(v):
    return float(v) * PT_MM


def mm_to_px(v, dpi):
    return float(v) * dpi / MM_PER_IN


def px_to_mm(v, dpi):
    return float(v) * MM_PER_IN / dpi


# --------------------------------------------------------------------------- #
# deterministic ids
# --------------------------------------------------------------------------- #
def stable_id(prefix, seed):
    """Deterministic short id: same (prefix, seed) -> same id, always."""
    h = hashlib.sha1(("%s|%s" % (prefix, seed)).encode("utf-8")).hexdigest()
    return "%s%s" % (prefix, h[:8])


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# spec loading / validation
# --------------------------------------------------------------------------- #
def load_spec(path):
    if not os.path.exists(path):
        raise SpecError("spec not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            spec = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SpecError("spec is not valid JSON: %s" % exc)
    issues = validate_spec(spec)
    errors = [i for i in issues if i["severity"] == "error"]
    if errors:
        raise SpecError("spec has %d error(s)" % len(errors), issues)
    return spec


def _issue(code, severity, path, message):
    return {"code": code, "severity": severity, "path": path, "message": message}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# --------------------------------------------------------------------------- #
# CONTRACT §6 chart fields — shared helpers (layout + datamap both use these, so
# a mark and its validation can never disagree about where x came from)
# --------------------------------------------------------------------------- #
def is_graphical_abstract(spec):
    """True only when the spec explicitly asks for the four-role layout."""
    layout_meta = (spec.get("meta") or {}).get("layout") \
        if isinstance(spec, dict) else None
    if isinstance(layout_meta, dict):
        return layout_meta.get("mode") == GRAPHICAL_ABSTRACT_MODE
    return False


def chart_x(chart):
    """Chart-level x values (``chart.x.values``), or [] when absent."""
    x = (chart or {}).get("x")
    if isinstance(x, dict) and isinstance(x.get("values"), list):
        return [v for v in x["values"] if _num(v)]
    return []


def series_values(series):
    vals = (series or {}).get("values")
    return [float(v) for v in vals if _num(v)] if isinstance(vals, list) else []


def series_x(chart, series, n=None):
    """x for one series, by the frozen priority:

    1. ``series[].x``            (CONTRACT §6 addendum, Lead 2026-11-23)
    2. ``chart.x.values``
    3. ``0 .. n-1``

    Always returns a list of exactly ``n`` floats (n defaults to len(values)).
    """
    vals = series_values(series)
    if n is None:
        n = len(vals)
    for candidate in ((series or {}).get("x"), chart_x(chart)):
        if isinstance(candidate, list) and len(candidate) == n \
                and all(_num(v) for v in candidate):
            return [float(v) for v in candidate]
    return [float(i) for i in range(n)]


def series_errors(series):
    errs = (series or {}).get("errors")
    return [float(e) for e in errs if _num(e)] if isinstance(errs, list) else []


def series_n(series, i=None):
    """Sample size of a series: int for the whole series or per-point list."""
    nv = (series or {}).get("n")
    if _num(nv):
        return int(nv)
    if isinstance(nv, list) and i is not None and i < len(nv) and _num(nv[i]):
        return int(nv[i])
    return None


def _check_series_fields(cp, si, s, issues):
    """Per-series §6 fields: errors / error_kind / n / x (E911, E913)."""
    sp_ = "%s.series[%d]" % (cp, si)
    vals = s.get("values")
    nvals = len(vals) if isinstance(vals, list) else 0

    errs = s.get("errors")
    kind = s.get("error_kind")
    if errs is not None:
        if not isinstance(errs, list) or not all(_num(e) for e in errs):
            issues.append(_issue("E911", "error", sp_ + ".errors",
                                 "errors must be a list of numbers (E911)"))
        elif isinstance(vals, list) and len(errs) != nvals:
            issues.append(_issue("E911", "error", sp_ + ".errors",
                                 "errors has %d value(s) but values has %d; they must be "
                                 "equal-length (E911)" % (len(errs), nvals)))
        if kind not in ERROR_KINDS:
            issues.append(_issue("E911", "warn", sp_ + ".error_kind",
                                 "errors without a declared error_kind %s -> got %r; the "
                                 "figure must state what the bars mean (E911)"
                                 % (list(ERROR_KINDS), kind)))
    elif kind is not None and kind not in ERROR_KINDS:
        issues.append(_issue("E911", "warn", sp_ + ".error_kind",
                             "unknown error_kind %r (want one of %s) (E911)"
                             % (kind, list(ERROR_KINDS))))

    nv = s.get("n")
    if nv is not None:
        if _num(nv):
            if int(nv) < 1:
                issues.append(_issue("E913", "error", sp_ + ".n",
                                     "n must be >= 1 (got %r) (E913)" % (nv,)))
        elif isinstance(nv, list):
            if not all(_num(v) and int(v) >= 1 for v in nv):
                issues.append(_issue("E913", "error", sp_ + ".n",
                                     "per-point n must all be integers >= 1 (E913)"))
            elif isinstance(vals, list) and len(nv) != nvals:
                issues.append(_issue("E913", "error", sp_ + ".n",
                                     "n has %d entry(ies) but values has %d (E913)"
                                     % (len(nv), nvals)))
        else:
            issues.append(_issue("E913", "error", sp_ + ".n",
                                 "n must be an int or a per-point list (E913)"))

    xs = s.get("x")
    if xs is not None:
        if not isinstance(xs, list) or not all(_num(v) for v in xs):
            issues.append(_issue("E054", "error", sp_ + ".x",
                                 "x must be a list of numbers"))
        elif isinstance(vals, list) and len(xs) != nvals:
            issues.append(_issue("E054", "error", sp_ + ".x",
                                 "x has %d value(s) but values has %d" % (len(xs), nvals)))


def _check_chart_fields(cp, chart, series, issues):
    """Chart-level §6 fields: categories / baseline / axis / significance / x_unit."""
    ctype = chart.get("type")
    cats = chart.get("categories")
    if cats is not None:
        if not isinstance(cats, list) or not all(isinstance(c, str) and c for c in cats):
            issues.append(_issue("E051", "error", cp + ".categories",
                                 "categories must be a list of non-empty strings"))
        elif len(set(cats)) != len(cats):
            issues.append(_issue("W051", "warn", cp + ".categories",
                                 "duplicate category label(s) in %r" % (cats,)))

    base = chart.get("baseline")
    if base is not None:
        if not _num(base):
            issues.append(_issue("E915", "error", cp + ".baseline",
                                 "baseline must be numeric (E915)"))
        elif ctype in BAR_LIKE_TYPES and float(base) != 0.0:
            just = chart.get("baseline_justification") or chart.get("justification")
            if not (isinstance(just, str) and just.strip()):
                issues.append(_issue(
                    "E915", "warn", cp + ".baseline",
                    "bars must sit on a zero baseline: baseline=%r for type %r with no "
                    "baseline_justification; the layout forces 0 and the figure must "
                    "say why (E915)" % (base, ctype)))

    axis = chart.get("axis")
    if axis is not None:
        _check_axis(cp, axis, issues)

    xs_unit = chart.get("x_unit")
    if xs_unit is not None and not (isinstance(xs_unit, str) and xs_unit.strip()):
        issues.append(_issue("E914", "warn", cp + ".x_unit", "x_unit must be a string"))

    # quantitative axis without any unit anywhere -> E914 (gate K fails it)
    if ctype in QUANT_AXIS_TYPES and series:
        y_unit = None
        x_unit = None
        if isinstance(axis, dict):
            y_unit = (axis.get("y") or {}).get("unit") if isinstance(axis.get("y"), dict) else None
            x_unit = (axis.get("x") or {}).get("unit") if isinstance(axis.get("x"), dict) else None
        y_unit = y_unit or chart.get("unit")
        x_unit = x_unit or chart.get("x_unit")
        if ctype == "barh":
            missing = not (x_unit or y_unit)
        else:
            missing = not y_unit
        if missing:
            issues.append(_issue("E914", "warn", cp + ".unit",
                                 "quantitative axis has no unit (chart.unit / axis.y.unit "
                                 "/ x_unit all empty) (E914)"))

    sig = chart.get("significance")
    if sig is not None:
        if not isinstance(sig, list):
            issues.append(_issue("E912", "error", cp + ".significance",
                                 "significance must be a list (E912)"))
        else:
            for gi, g in enumerate(sig):
                gp = "%s.significance[%d]" % (cp, gi)
                if not isinstance(g, dict):
                    issues.append(_issue("E912", "error", gp, "significance item must be an object (E912)"))
                    continue
                between = g.get("between")
                if not (isinstance(between, list) and len(between) == 2
                        and all(isinstance(b, str) and b for b in between)):
                    issues.append(_issue("E912", "error", gp + ".between",
                                         "between must be exactly two non-empty category "
                                         "labels (E912)"))
                if not (isinstance(g.get("label"), str) and g.get("label").strip()):
                    issues.append(_issue("E912", "error", gp + ".label",
                                         "significance needs a label string (** / ns / p=...) (E912)"))
                p = g.get("p")
                defined = bool(g.get("defined_in_caption"))
                if p is not None and not _num(p):
                    issues.append(_issue("E912", "warn", gp + ".p", "p must be numeric (E912)"))
                elif isinstance(p, (int, float)) and not isinstance(p, bool) \
                        and not (0.0 <= float(p) <= 1.0):
                    issues.append(_issue("E912", "warn", gp + ".p",
                                         "p=%r is outside [0,1] (E912)" % (p,)))
                if not (g.get("test") or g.get("defined_in_caption") or defined):
                    issues.append(_issue("E912", "warn", gp,
                                         "significance is not defined: declare test+p or "
                                         "defined_in_caption=true (E912)"))


def _check_axis(cp, axis, issues):
    """axis.x / axis.y: label, unit, scale, tick_start/end/step (E914)."""
    if not isinstance(axis, dict):
        issues.append(_issue("E914", "error", cp + ".axis", "axis must be an object (E914)"))
        return
    for key in ("x", "y"):
        ax = axis.get(key)
        if ax is None:
            continue
        ap = "%s.axis.%s" % (cp, key)
        if not isinstance(ax, dict):
            issues.append(_issue("E914", "error", ap, "axis.%s must be an object (E914)" % key))
            continue
        for field in ("label", "unit"):
            v = ax.get(field)
            if v is not None and not isinstance(v, str):
                issues.append(_issue("E914", "warn", "%s.%s" % (ap, field),
                                     "axis.%s.%s must be a string (E914)" % (key, field)))
        scale = ax.get("scale")
        if scale is not None and scale not in AXIS_SCALES:
            issues.append(_issue("E914", "error", ap + ".scale",
                                 "scale must be one of %s (got %r) (E914)"
                                 % (list(AXIS_SCALES), scale)))
        lo, hi, step = ax.get("tick_start"), ax.get("tick_end"), ax.get("tick_step")
        for field, v in (("tick_start", lo), ("tick_end", hi), ("tick_step", step)):
            if v is not None and not _num(v):
                issues.append(_issue("E914", "error", "%s.%s" % (ap, field),
                                     "%s must be numeric (E914)" % field))
        if _num(lo) and _num(hi) and float(hi) <= float(lo):
            issues.append(_issue("E914", "error", ap,
                                 "tick_end %r must be > tick_start %r (E914)" % (hi, lo)))
        if _num(step) and float(step) <= 0:
            issues.append(_issue("E914", "error", ap + ".tick_step",
                                 "tick_step must be > 0 (E914)"))
        if scale == "log10" and _num(lo) and float(lo) <= 0:
            issues.append(_issue("E914", "error", ap + ".tick_start",
                                 "log10 axis cannot start at %r (E914)" % (lo,)))


def validate_spec(spec):
    """Structural validation only. Geometric checks live in validate.py."""
    issues = []
    if not isinstance(spec, dict):
        return [_issue("E000", "error", "$", "spec must be a JSON object")]

    meta = spec.get("meta")
    if not isinstance(meta, dict):
        issues.append(_issue("E001", "error", "$.meta", "missing meta object"))
        meta = {}
    if not isinstance(meta.get("case_id"), str) or not meta.get("case_id"):
        issues.append(_issue("E002", "error", "$.meta.case_id", "case_id must be a non-empty string"))

    canvas = meta.get("canvas") or {}
    merged = dict(DEFAULT_CANVAS)
    merged.update({k: v for k, v in canvas.items() if _num(v)})
    if not (20 <= merged["w_mm"] <= 1000):
        issues.append(_issue("E003", "error", "$.meta.canvas.w_mm", "w_mm out of range 20..1000"))
    if not (20 <= merged["h_mm"] <= 1000):
        issues.append(_issue("E004", "error", "$.meta.canvas.h_mm", "h_mm out of range 20..1000"))
    if not (72 <= merged["dpi"] <= 1200):
        issues.append(_issue("E005", "error", "$.meta.canvas.dpi", "dpi out of range 72..1200"))
    if not (0 <= merged["margin_mm"] <= 20):
        issues.append(_issue("E006", "error", "$.meta.canvas.margin_mm", "margin_mm out of range 0..20"))
    if not (0 < merged["margin_mm"] * 2 < min(merged["w_mm"], merged["h_mm"])):
        issues.append(_issue("E007", "error", "$.meta.canvas", "margins leave no drawing area"))

    pal = spec.get("palette") or {}
    pid = pal.get("id") if isinstance(pal, dict) else None
    if pid is None:
        issues.append(_issue("W010", "warn", "$.palette.id", "no palette id; defaulting to okabe-ito"))
    elif pid not in PALETTES and not isinstance(pal.get("colors"), list):
        issues.append(_issue("E011", "error", "$.palette.id", "unknown palette %r and no colors[] given" % (pid,)))

    if not isinstance(meta.get("language"), str):
        issues.append(_issue("W012", "warn", "$.meta.language", "language not set; fonts will auto-resolve"))

    evidence = spec.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        issues.append(_issue("E020", "error", "$.evidence", "evidence list must be non-empty"))
        evidence = []
    seen_ev = set()
    for i, ev in enumerate(evidence):
        p = "$.evidence[%d]" % i
        if not isinstance(ev, dict):
            issues.append(_issue("E021", "error", p, "evidence item must be an object"))
            continue
        eid = ev.get("id")
        if not isinstance(eid, str) or not eid:
            issues.append(_issue("E022", "error", p + ".id", "evidence id required"))
        elif eid in seen_ev:
            issues.append(_issue("E023", "error", p + ".id", "duplicate evidence id %r" % eid))
        else:
            seen_ev.add(eid)
        if not isinstance(ev.get("claim"), str) or not ev.get("claim"):
            issues.append(_issue("E024", "error", p + ".claim", "claim required"))
        src = ev.get("source")
        if not isinstance(src, dict):
            issues.append(_issue("E025", "error", p + ".source", "source object required"))
        else:
            for k in ("file", "locator"):
                if not isinstance(src.get(k), str) or not src.get(k):
                    issues.append(_issue("E026", "error", p + ".source." + k, "source.%s required" % k))
            if not isinstance(src.get("quote"), str) or not src.get("quote").strip():
                issues.append(_issue("E027", "error", p + ".source.quote", "verbatim quote required"))
        if not isinstance(ev.get("kind"), str):
            issues.append(_issue("W028", "warn", p + ".kind", "kind missing (entity|relation|number|condition)"))

    panels = spec.get("panels")
    if not isinstance(panels, list) or not panels:
        issues.append(_issue("E030", "error", "$.panels", "at least one panel required"))
        return issues

    seen_panel = set()
    seen_ids = set()
    for pi, panel in enumerate(panels):
        pp = "$.panels[%d]" % pi
        if not isinstance(panel, dict):
            issues.append(_issue("E031", "error", pp, "panel must be an object"))
            continue
        pid_ = panel.get("id")
        if not isinstance(pid_, str) or not pid_:
            issues.append(_issue("E032", "error", pp + ".id", "panel id required"))
        elif pid_ in seen_panel:
            issues.append(_issue("E033", "error", pp + ".id", "duplicate panel id %r" % pid_))
        else:
            seen_panel.add(pid_)

        role = panel.get("role")
        ga_mode = is_graphical_abstract(spec)
        if role in (None, ""):
            # Data-chart panels do not need a diagrammatic role.  Only the
            # graphical-abstract layout mode requires the four roles, so a
            # missing role is an error there and merely silent here.
            if ga_mode:
                issues.append(_issue("E034", "error", pp + ".role",
                                     "role must be one of %s (got %r)" % (list(PANEL_ROLES), role)))
        elif not isinstance(role, str):
            issues.append(_issue("E034", "error", pp + ".role", "role must be a string"))
        elif role not in PANEL_ROLES and ga_mode:
            issues.append(_issue("E034", "warn", pp + ".role",
                                 "role %r is not one of %s (required by layout.mode=%r)"
                                 % (role, list(PANEL_ROLES), GRAPHICAL_ABSTRACT_MODE)))
        if panel.get("title") in (None, ""):
            if ga_mode:
                issues.append(_issue("E035", "error", pp + ".title", "panel title required"))
            else:
                issues.append(_issue("W035", "warn", pp + ".title",
                                     "panel has no title; the layout falls back to its id"))

        nodes = panel.get("nodes") or []
        if not isinstance(nodes, list):
            issues.append(_issue("E036", "error", pp + ".nodes", "nodes must be a list"))
            nodes = []
        local = set()
        for ni, node in enumerate(nodes):
            np_ = "%s.nodes[%d]" % (pp, ni)
            if not isinstance(node, dict):
                issues.append(_issue("E037", "error", np_, "node must be an object"))
                continue
            nid = node.get("id")
            if not isinstance(nid, str) or not nid:
                issues.append(_issue("E038", "error", np_ + ".id", "node id required"))
            elif nid in seen_ids:
                issues.append(_issue("E039", "error", np_ + ".id", "node id used twice in spec: %r" % nid))
            else:
                seen_ids.add(nid)
                local.add(nid)
            if not isinstance(node.get("label"), str) or not node.get("label"):
                issues.append(_issue("E040", "error", np_ + ".label", "node label required"))
            shape = node.get("shape", "rrect")
            if shape not in SHAPES:
                issues.append(_issue("E041", "warn", np_ + ".shape", "unknown shape %r -> rrect" % (shape,)))
            if not (node.get("source") or node.get("sources")):
                issues.append(_issue("W042", "warn", np_ + ".source", "node has no evidence source"))

        for ei, edge in enumerate(panel.get("edges") or []):
            ep = "%s.edges[%d]" % (pp, ei)
            if not isinstance(edge, dict):
                issues.append(_issue("E043", "error", ep, "edge must be an object"))
                continue
            if not isinstance(edge.get("id"), str) or not edge.get("id"):
                issues.append(_issue("E044", "error", ep + ".id", "edge id required"))
            for k in ("from", "to"):
                ref = edge.get(k)
                if not isinstance(ref, str) or not ref:
                    issues.append(_issue("E045", "error", ep + "." + k, "edge endpoint required"))
                elif ref not in local:
                    issues.append(_issue("E046", "error", ep + "." + k,
                                         "edge endpoint %r not a node in this panel" % (ref,)))
            kind = edge.get("kind")
            if kind is not None and kind not in EDGE_KINDS:
                issues.append(_issue("E047", "error", ep + ".kind",
                                     "kind must be one of %s (got %r)" % (list(EDGE_KINDS), kind)))
            if not (edge.get("source") or edge.get("sources")):
                issues.append(_issue("W048", "warn", ep + ".source", "edge has no evidence source"))

        for ci, chart in enumerate(panel.get("charts") or []):
            cp = "%s.charts[%d]" % (pp, ci)
            if not isinstance(chart, dict):
                issues.append(_issue("E049", "error", cp, "chart must be an object"))
                continue
            ctype = chart.get("type")
            if ctype not in CHART_TYPES:
                issues.append(_issue("E050", "error", cp + ".type",
                                     "chart type must be one of %s" % sorted(CHART_TYPES)))
            series = chart.get("series")
            if not isinstance(series, list) or not series:
                issues.append(_issue("E051", "error", cp + ".series", "chart series required"))
            else:
                for si, s in enumerate(series):
                    if not isinstance(s, dict):
                        issues.append(_issue("E052", "error", "%s.series[%d]" % (cp, si), "series item must be object"))
                        continue
                    sp_ = "%s.series[%d]" % (cp, si)
                    if not isinstance(s.get("name"), str):
                        issues.append(_issue("E053", "error", sp_ + ".name", "series name required"))
                    vals = s.get("values")
                    if not isinstance(vals, list) or not vals:
                        issues.append(_issue("E054", "error", sp_ + ".values", "values required"))
                    elif not all(_num(v) for v in vals):
                        issues.append(_issue("E055", "error", sp_ + ".values",
                                             "all values must be numbers"))
                    if not (s.get("source") or s.get("sources")):
                        issues.append(_issue("W056", "warn", sp_ + ".source",
                                             "series has no evidence source"))
                    _check_series_fields(cp, si, s, issues)
            _check_chart_fields(cp, chart, series if isinstance(series, list) else [], issues)
            if not chart.get("unit"):
                issues.append(_issue("W057", "warn", cp + ".unit", "chart has no unit"))

        legend = panel.get("legend")
        if legend is not None:
            if not isinstance(legend, dict):
                issues.append(_issue("E058", "error", pp + ".legend", "legend must be an object"))
            elif not isinstance(legend.get("items"), list):
                issues.append(_issue("E059", "error", pp + ".legend.items", "legend items list required"))

        for bi, cb in enumerate(panel.get("colorbars") or []):
            bp = "%s.colorbars[%d]" % (pp, bi)
            if not isinstance(cb, dict):
                issues.append(_issue("E060", "error", bp, "colorbar must be an object"))
                continue
            for k in ("vmin", "vmax"):
                if not _num(cb.get(k)):
                    issues.append(_issue("E061", "error", bp + "." + k, "%s must be numeric" % k))
            if _num(cb.get("vmin")) and _num(cb.get("vmax")) and cb["vmax"] <= cb["vmin"]:
                issues.append(_issue("E062", "error", bp, "vmax must be > vmin"))
            if cb.get("cmap") not in CMAPS:
                issues.append(_issue("E063", "error", bp + ".cmap", "cmap must be one of %s" % sorted(CMAPS)))
            if not cb.get("unit"):
                issues.append(_issue("W064", "warn", bp + ".unit", "colorbar has no unit"))

    # the four-role coverage requirement belongs to the graphical-abstract
    # layout mode only: a data-chart figure is not penalised for lacking
    # problem/method/results/conclusion panels (chart-engine task item 1).
    if is_graphical_abstract(spec):
        roles = [p.get("role") for p in panels if isinstance(p, dict)]
        for required in PANEL_ROLES:
            if required not in roles:
                issues.append(_issue("W070", "warn", "$.panels",
                                     "layout.mode=%r requires a panel with role %r "
                                     "(four-element coverage)" % (GRAPHICAL_ABSTRACT_MODE, required)))
    return issues


def resolve_canvas(meta):
    canvas = dict(DEFAULT_CANVAS)
    canvas.update({k: float(v) for k, v in ((meta or {}).get("canvas") or {}).items() if _num(v)})
    return canvas


def resolve_palette(spec):
    pal = spec.get("palette") or {}
    colors = pal.get("colors")
    if isinstance(colors, list) and colors:
        return [c for c in colors if isinstance(c, str)]
    pid = pal.get("id") or "okabe-ito"
    return list(PALETTES.get(pid, PALETTES["okabe-ito"]))


def evidence_ids(spec):
    return {e.get("id") for e in (spec.get("evidence") or []) if isinstance(e, dict)}


_WS = re.compile(r"\s+")


def norm_ws(text):
    return _WS.sub(" ", (text or "")).strip()

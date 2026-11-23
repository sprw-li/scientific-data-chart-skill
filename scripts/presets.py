#!/usr/bin/env python3
"""presets.py — journal/style presets (CONTRACT §8).

Frozen API used by ``chart.py``::

    ids()                    -> sorted list of preset ids
    available()              -> alias of ids()
    get(preset_id)           -> preset dict (raises KeyError when unknown)
    apply(spec, preset_id)   -> (new_spec, resolved_dict)
    list_presets()           -> JSON-able catalogue for ``chart.py presets --list``

The preset *data* lives in ``presets/*.json`` (one file per preset) so that a
reviewer can read and contest the numbers without reading Python.  Every file
carries ``source`` (where the numbers come from) and ``param_source`` (a per
parameter-group classification: ``guideline`` when the value is stated by the
publisher, ``convention`` when it is a working default of ours).

Rules that are deliberately enforced here:

* An unknown preset id raises ``KeyError`` — never a silent fall-back
  (CONTRACT §8 / E916).  ``chart.py`` turns that into exit 4 + ``spec-issues.json``.
* ``apply`` is a pure function of (spec, preset file): the same input pair yields
  a byte-identical spec, so the preset cannot introduce non-determinism.
* Type sizes are clamped to the contract floors (11 / 8 / 7 pt) and the clamps are
  recorded in ``resolved.typography_clamped`` — a preset that asks for 5 pt type
  must not quietly win over gate B.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import json
import os

import scene

HERE = os.path.dirname(os.path.abspath(__file__))
PRESET_DIR = os.path.join(os.path.dirname(HERE), "presets")

# Type-size key -> contract floor (pt).  scene.py owns the numbers.
SIZE_FLOORS = {
    "main_title": scene.MIN_MAIN_TITLE_PT,
    "panel_title": scene.MIN_PANEL_TITLE_PT,
    "body": scene.MIN_BODY_PT,
    "label": scene.MIN_BODY_PT,
    "legend": scene.MIN_BODY_PT,
}

# preset.font_sizes_pt key -> layout.BASE_SIZES key
_FONT_MAP = (
    ("main_title", "title"),
    ("body", "subtitle"),
    ("body", "caption"),
    ("panel_title", "panel_title"),
    ("body", "panel_role"),
    ("body", "node_label"),
    ("body", "node_sub"),
    ("body", "edge_label"),
    ("label", "axis"),
    ("label", "chart_title"),
    ("legend", "legend"),
    ("legend", "colorbar"),
)

DEFAULT_ID = "generic"
REQUIRED_FIELDS = (
    "id", "name", "canvas_mm", "dpi", "font_sizes_pt", "line_widths_pt",
    "palette_id", "margins_mm", "panel_label_style", "font_family_stack",
    "cjk_fallback_stack", "tick_style", "notes", "source", "param_source",
)

_CACHE = {}


def _preset_files():
    if not os.path.isdir(PRESET_DIR):
        return []
    names = [n for n in os.listdir(PRESET_DIR)
             if n.endswith(".json") and os.path.isfile(os.path.join(PRESET_DIR, n))]
    return [os.path.join(PRESET_DIR, n) for n in sorted(names)]


def _load_all():
    """Load every presets/*.json.  Deterministic: sorted by filename, cached."""
    if _CACHE:
        return _CACHE
    out = {}
    for path in _preset_files():
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (ValueError, OSError):
            continue                      # an unreadable preset is simply absent
        if not isinstance(data, dict):
            continue
        pid = data.get("id") or os.path.splitext(os.path.basename(path))[0]
        if not isinstance(pid, str) or not pid:
            continue
        data["id"] = pid
        data["file"] = os.path.basename(path)
        missing = [k for k in REQUIRED_FIELDS if k not in data]
        if missing:
            # report rather than guess: a preset missing its frozen fields is not
            # used, which keeps "preset applied" honest.
            data["invalid"] = "missing field(s): %s" % ", ".join(missing)
        out[pid] = data
    _CACHE.clear()
    _CACHE.update(out)
    return out


def ids():
    """Sorted ids of the usable presets (deterministic)."""
    return sorted(pid for pid, p in _load_all().items() if not p.get("invalid"))


def available():
    return ids()


def get(preset_id):
    """The preset dict for `preset_id`.  Raises KeyError when unknown/invalid."""
    if not isinstance(preset_id, str) or not preset_id:
        raise KeyError("preset id must be a non-empty string")
    table = _load_all()
    if preset_id not in table:
        raise KeyError(preset_id)
    preset = table[preset_id]
    if preset.get("invalid"):
        raise KeyError("%s (%s)" % (preset_id, preset["invalid"]))
    return preset


def list_presets():
    """Catalogue rows for ``chart.py presets --list`` (JSON serialisable)."""
    rows = []
    for pid in ids():
        p = _load_all()[pid]
        canvas = p.get("canvas_mm") or {}
        rows.append({
            "id": pid,
            "name": p.get("name"),
            "file": p.get("file"),
            "canvas_mm": {"single": canvas.get("single"), "double": canvas.get("double"),
                          "height": canvas.get("height")},
            "default_columns": p.get("default_columns", 1),
            "dpi": p.get("dpi"),
            "font_sizes_pt": p.get("font_sizes_pt"),
            "line_widths_pt": p.get("line_widths_pt"),
            "palette_id": p.get("palette_id"),
            "margins_mm": p.get("margins_mm"),
            "panel_label_style": p.get("panel_label_style"),
            "font_family_stack": p.get("font_family_stack"),
            "cjk_fallback_stack": p.get("cjk_fallback_stack"),
            "tick_style": p.get("tick_style"),
            "notes": p.get("notes"),
            "source": p.get("source"),
            "param_source": p.get("param_source"),
        })
    return rows


def _pick_columns(spec, preset):
    meta = spec.get("meta") or {}
    opts = meta.get("preset_options") if isinstance(meta.get("preset_options"), dict) else {}
    cols = opts.get("columns")
    if not isinstance(cols, int) or cols < 1:
        cols = preset.get("default_columns", 1)
    return 1 if int(cols) < 2 else 2


def resolve_canvas(spec, preset, columns=None):
    """Canvas the preset implies.

    Precedence (documented so a reviewer can contest it):
      * **width** comes from the preset (single/double column) — that is the
        journal constraint the preset exists for; ``meta.preset_options.columns``
        selects the double-column width.
      * **height** stays the figure's own decision: an explicit
        ``meta.canvas.h_mm`` wins, otherwise the preset default is used.
      * ``margin_mm`` is the largest per-side margin the journal states, so the
        uniform margin used by the layout engine cannot violate a side.
      * ``dpi`` comes from the preset.
    """
    canvas_mm = preset.get("canvas_mm") or {}
    meta = spec.get("meta") or {}
    user = meta.get("canvas") if isinstance(meta.get("canvas"), dict) else {}
    columns = columns or _pick_columns(spec, preset)
    key = "double" if columns >= 2 else "single"
    width = canvas_mm.get(key) or canvas_mm.get("width")
    height = canvas_mm.get("height")
    margins = preset.get("margins_mm") or {}
    sides = []
    if isinstance(margins, dict):
        for side in ("left", "right", "top", "bottom"):
            v = margins.get(side)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                sides.append(float(v))
    elif isinstance(margins, (int, float)) and not isinstance(margins, bool):
        sides.append(float(margins))
    out = {}
    if isinstance(width, (int, float)):
        out["w_mm"] = float(width)
    if isinstance(user.get("h_mm"), (int, float)) and not isinstance(user.get("h_mm"), bool):
        out["h_mm"] = float(user["h_mm"])
    elif isinstance(height, (int, float)):
        out["h_mm"] = float(height)
    out["margin_mm"] = max(sides) if sides else float(scene.DEFAULT_CANVAS["margin_mm"])
    dpi = preset.get("dpi")
    if isinstance(dpi, (int, float)) and not isinstance(dpi, bool):
        out["dpi"] = int(dpi)
    return out


def resolve_typography(preset):
    """(sizes_by_layout_key, clamps) for the preset, clamped to contract floors."""
    src = preset.get("font_sizes_pt") or {}
    sizes, clamped = {}, {}
    for src_key, layout_key in _FONT_MAP:
        v = src.get(src_key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        floor = SIZE_FLOORS.get(src_key)
        if floor is not None and float(v) < floor:
            clamped[src_key] = {"asked_pt": float(v), "used_pt": float(floor),
                                "floor": floor}
            v = floor
        sizes[layout_key] = round(float(v), 2)
    return sizes, clamped


def apply(spec, preset_id):
    """Apply a preset to a spec copy.

    Returns ``(new_spec, resolved)``.  ``new_spec`` carries:
      * ``meta.preset`` / ``meta.preset_values`` (the full preset, for
        ``figure_spec.resolved.json``),
      * ``meta.canvas`` merged from the preset (width from the chosen column
        count, height kept when the spec asked for one),
      * ``meta.typography`` — the per-key type sizes layout must use,
      * ``meta.panel_label_style`` and ``meta.tick_style``,
      * ``palette.id`` when the spec did not pin explicit colours.
    Raises ``KeyError`` for an unknown preset id (E916).
    """
    preset = get(preset_id)
    new_spec = json.loads(json.dumps(spec, ensure_ascii=False))
    meta = new_spec.setdefault("meta", {})
    if not isinstance(meta, dict):
        meta = {}
        new_spec["meta"] = meta

    columns = _pick_columns(new_spec, preset)
    canvas = resolve_canvas(new_spec, preset, columns)
    merged = dict(scene.DEFAULT_CANVAS)
    cur = meta.get("canvas") if isinstance(meta.get("canvas"), dict) else {}
    merged.update({k: v for k, v in cur.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)})
    merged.update(canvas)
    meta["canvas"] = merged

    sizes, clamped = resolve_typography(preset)
    meta["typography"] = sizes
    meta["preset"] = preset["id"]
    meta["panel_label_style"] = preset.get("panel_label_style")
    meta["tick_style"] = preset.get("tick_style")
    if isinstance(preset.get("line_widths_pt"), dict):
        meta["line_widths_pt"] = dict(preset["line_widths_pt"])
    meta["preset_values"] = preset

    pal = new_spec.get("palette")
    if not isinstance(pal, dict):
        pal = {}
        new_spec["palette"] = pal
    if not isinstance(pal.get("colors"), list) or not pal.get("colors"):
        if preset.get("palette_id"):
            pal["id"] = preset["palette_id"]
    pal.setdefault("id", preset.get("palette_id") or "okabe-ito")

    resolved = {
        "id": preset["id"],
        "name": preset.get("name"),
        "file": preset.get("file"),
        "columns": columns,
        "canvas_mm": canvas,
        "typography_pt": sizes,
        "typography_clamped": clamped,
        "dpi": canvas.get("dpi"),
        "palette_id": pal.get("id"),
        "source": preset.get("source"),
        "param_source": preset.get("param_source"),
        "notes": preset.get("notes"),
        "values": preset,
    }
    return new_spec, resolved


def settings_for(spec):
    """Typographic/spacing settings layout should use for this spec.

    Reads what :func:`apply` wrote; returns a plain dict so layout does not need
    to import this module (and no import cycle can appear).
    """
    meta = spec.get("meta") or {}
    sizes = meta.get("typography") if isinstance(meta.get("typography"), dict) else {}
    plabel = meta.get("panel_label_style") \
        if isinstance(meta.get("panel_label_style"), dict) else {}
    ticks = meta.get("tick_style") if isinstance(meta.get("tick_style"), dict) else {}
    return {"typography": dict(sizes), "panel_label_style": dict(plabel),
            "tick_style": dict(ticks), "preset": meta.get("preset")}

#!/usr/bin/env python3
"""svg_backend.py — scene -> layered, editable, deterministic SVG (contract §4, §6 F).

    write_svg(scene, path, fs, spec) -> {"path","bytes","text_nodes","groups","sha256"}

The SVG is a genuine editing source, not a rendered picture:
    * every label is a real <text> element (never a <path>) in the resolved
      font stack, one element per wrapped line;
    * elements are grouped per semantic layer, <g id="layer-...">;
    * every element has a unique id plus data-element / data-layer /
      data-evidence / data-panel attributes, and data-value where the element
      encodes a number;
    * a <metadata> block carries generator, spec sha256, canvas, palette, fonts;
    * width/height are in mm and viewBox matches (1 user unit = 1 mm);
    * serialisation is byte-stable: fixed attribute order, fixed float
      precision, no timestamps, no ids derived from hash randomisation.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import hashlib
import json
import os

LAYER_ORDER = ["frame", "panel", "chart", "entity", "relation",
               "legend", "colorbar", "annotation"]

_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"))


def esc(value):
    out = str(value)
    for a, b in _ESCAPES:
        out = out.replace(a, b)
    return out


def num(v):
    """Fixed-precision mm/number formatting, with -0 normalised to 0."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "0"
    if f == 0.0:
        f = 0.0
    s = "%.4f" % f
    s = s.rstrip("0").rstrip(".")
    return s if s not in ("", "-0", "-") else "0"


def _dash(value):
    return " ".join(num(v) for v in value)


def _attrs(pairs):
    """Deterministic attribute serialisation (caller controls the order)."""
    out = []
    for key, value in pairs:
        if value is None:
            continue
        out.append('%s="%s"' % (key, esc(value)))
    return (" " + " ".join(out)) if out else ""


# --------------------------------------------------------------------------- #
# style helpers
# --------------------------------------------------------------------------- #
def _paint(style, default_fill="none", default_stroke="none"):
    fill = style.get("fill", default_fill)
    stroke = style.get("stroke", default_stroke)
    pairs = []
    if fill is not None:
        pairs.append(("fill", fill))
    if stroke:
        pairs.append(("stroke", stroke))
    width = style.get("stroke_width_mm")
    if stroke and width is not None:
        pairs.append(("stroke-width", num(width)))
    if stroke and style.get("dash"):
        pairs.append(("stroke-dasharray", _dash(style["dash"])))
    if style.get("linecap"):
        pairs.append(("stroke-linecap", style["linecap"]))
    if style.get("linejoin"):
        pairs.append(("stroke-linejoin", style["linejoin"]))
    if style.get("opacity") is not None:
        pairs.append(("opacity", num(style["opacity"])))
    return pairs


def _bounds_attrs(prim):
    b = prim.get("bbox") or {}
    return _attrs([("x", num(b.get("x", 0))), ("y", num(b.get("y", 0))),
                   ("width", num(b.get("w", 0))), ("height", num(b.get("h", 0)))])


# --------------------------------------------------------------------------- #
# element writers
# --------------------------------------------------------------------------- #
def _common_pairs(prim, extra=None):
    pairs = [
        ("id", prim.get("id")),
        ("data-element", prim.get("element")),
        ("data-layer", prim.get("layer")),
        ("data-type", prim.get("type")),
    ]
    if prim.get("panel"):
        pairs.append(("data-panel", prim["panel"]))
    if prim.get("evidence"):
        pairs.append(("data-evidence", prim["evidence"]))
    for key in ("edge_id", "edge_kind", "node_id", "chart_id", "colorbar_id",
                "series_name", "category", "legend"):
        if prim.get(key) is not None:
            pairs.append(("data-" + key.replace("_", "-"), prim[key]))
    if prim.get("value") is not None:
        pairs.append(("data-value", num(prim["value"])))
    if prim.get("unit"):
        pairs.append(("data-unit", prim["unit"]))
    if prim.get("source"):
        src = prim["source"] or {}
        if src.get("file"):
            pairs.append(("data-source-file", src["file"]))
        if src.get("locator"):
            pairs.append(("data-source-locator", src["locator"]))
    if extra:
        pairs.extend(extra)
    return pairs


def _write_shape(out, prim):
    ptype = prim.get("type")
    style = prim.get("style") or {}
    paint = _paint(style)
    common = _attrs(_common_pairs(prim))
    if ptype == "rect" or ptype == "rrect":
        rx = style.get("rx_mm")
        extra = _attrs([("rx", num(rx))]) if rx else ""
        out.append("<rect%s%s%s%s/>" % (common, extra, _bounds_attrs(prim), _attrs(paint)))
    elif ptype == "circle":
        out.append("<circle%s%s/>" % (
            common,
            _attrs([("cx", num(prim.get("cx", 0))), ("cy", num(prim.get("cy", 0))),
                    ("r", num(prim.get("r", 0)))] + paint)))
    elif ptype == "ellipse":
        out.append("<ellipse%s%s/>" % (
            common,
            _attrs([("cx", num(prim.get("cx", 0))), ("cy", num(prim.get("cy", 0))),
                    ("rx", num(prim.get("rx", 0))), ("ry", num(prim.get("ry", 0)))]
                   + paint)))
    elif ptype == "line":
        pts = prim.get("points") or []
        if len(pts) >= 2:
            out.append("<line%s%s/>" % (
                common,
                _attrs([("x1", num(pts[0][0])), ("y1", num(pts[0][1])),
                        ("x2", num(pts[1][0])), ("y2", num(pts[1][1]))] + paint)))
    elif ptype == "polygon":
        pts = " ".join("%s,%s" % (num(p[0]), num(p[1]))
                       for p in (prim.get("points") or []))
        out.append("<polygon%s%s/>" % (
            common, _attrs([("points", pts)] + paint)))
    elif ptype == "path":
        d = prim.get("d") or ""
        if not d and prim.get("points"):
            pts = prim["points"]
            d = "M %s %s" % (num(pts[0][0]), num(pts[0][1]))
            for p in pts[1:]:
                d += " L %s %s" % (num(p[0]), num(p[1]))
        out.append("<path%s%s/>" % (common, _attrs([("d", d)] + paint)))
    elif ptype == "image":
        href = prim.get("href") or ""
        out.append("<image%s%s/>" % (
            common, _bounds_attrs(prim) + _attrs([("href", href)])))


def _write_text(out, prim, family_css):
    style = prim.get("style") or {}
    size = float(style.get("font_size_pt") or prim.get("font_size_pt") or 7.0)
    lines = prim.get("lines") or []
    if prim.get("text") and not lines:
        lines = [prim["text"]]
    baselines = prim.get("line_baselines") or []
    anchor = style.get("anchor") or "start"
    x = prim.get("line_x")
    if x is None:
        x = (prim.get("bbox") or {}).get("x", 0)
    rotate = style.get("rotate_deg")
    weight = "bold" if style.get("bold") else "normal"
    colour = style.get("color") or "#111111"
    for i, line in enumerate(lines):
        y = baselines[i] if i < len(baselines) else (prim.get("bbox") or {}).get("y", 0)
        extra = [("font-family", family_css), ("font-size", num(size)),
                 ("font-weight", weight), ("fill", colour), ("text-anchor", anchor),
                 ("xml:space", "preserve")]
        if rotate:
            extra.append(("transform", "rotate(%s %s %s)"
                          % (num(rotate), num(x), num(y))))
        inner = _common_pairs(prim, [("data-line", i), ("data-lines", len(lines))])
        if len(lines) > 1:
            inner = [("id", "%s-l%d" % (prim["id"], i))] + \
                [p for p in inner if p[0] != "id"]
        out.append("<text%s%s>%s</text>"
                   % (_attrs(inner), _attrs(extra), esc(line)))
    return len(lines)


# --------------------------------------------------------------------------- #
# metadata
# --------------------------------------------------------------------------- #
def _metadata_xml(scene, spec, fs, text_nodes):
    canvas = scene.get("canvas") or {}
    meta = scene.get("meta") or {}
    payload = {
        "generator": meta.get("generator", "graphical-abstract/svg_backend.py"),
        "scene_version": meta.get("version"),
        "spec_sha256": meta.get("spec_sha256"),
        "canvas": {"w_mm": canvas.get("w_mm"), "h_mm": canvas.get("h_mm"),
                   "margin_mm": canvas.get("margin_mm"), "dpi": canvas.get("dpi")},
        "palette": list(meta.get("palette") or []),
        "palette_id": meta.get("palette_id"),
        "language": meta.get("language"),
        "fonts": {
            "family_css": getattr(fs, "family_css", "sans-serif"),
            "regular": os.path.basename(getattr(fs, "regular", "") or ""),
            "cjk": os.path.basename(getattr(fs, "cjk", "") or ""),
            "degraded": bool(getattr(fs, "degraded", False)),
        },
        "layers": list(LAYER_ORDER),
        "counts": {"primitives": len(scene.get("primitives") or []),
                   "text_nodes": text_nodes,
                   "panels": len(scene.get("panels") or [])},
        "units": "mm, origin top-left, y down (1 SVG user unit = 1 mm)",
        "case_id": (spec.get("meta") or {}).get("case_id"),
        "evidence_ids": sorted(e.get("id") for e in (spec.get("evidence") or [])
                               if isinstance(e, dict) and isinstance(e.get("id"), str)),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    # CDATA keeps the JSON byte-exact for machine readers (attribute escaping
    # would turn every quote into &quot;).
    body = body.replace("]]>", "]]]]><![CDATA[>")
    return ('<metadata id="ga-metadata" data-generator="graphical-abstract">'
            "<![CDATA[%s]]></metadata>" % body)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def write_svg(scene, path, fs, spec):
    """Serialise `scene` to an editable SVG. Returns a metrics dict."""
    canvas = scene.get("canvas") or {}
    W = float(canvas.get("w_mm", 180.0))
    H = float(canvas.get("h_mm", 120.0))
    family_css = getattr(fs, "family_css", None) or "sans-serif"
    prims = list(scene.get("primitives") or [])

    body = []
    text_nodes = 0
    groups = 0
    for layer in LAYER_ORDER:
        members = [p for p in prims if p.get("layer") == layer]
        if not members:
            continue
        groups += 1
        body.append('<g id="layer-%s" data-layer="%s" class="layer">' % (layer, layer))
        for prim in members:
            if prim.get("type") == "text":
                text_nodes += _write_text(body, prim, family_css)
            else:
                _write_shape(body, prim)
        body.append("</g>")
    others = [p for p in prims if p.get("layer") not in LAYER_ORDER]
    if others:
        groups += 1
        body.append('<g id="layer-other" data-layer="other" class="layer">')
        for prim in others:
            if prim.get("type") == "text":
                text_nodes += _write_text(body, prim, family_css)
            else:
                _write_shape(body, prim)
        body.append("</g>")

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
         'width="%smm" height="%smm" viewBox="0 0 %s %s" '
         'data-generator="graphical-abstract/svg_backend.py" '
         'data-units="mm">' % (num(W), num(H), num(W), num(H))),
        _metadata_xml(scene, spec, fs, text_nodes),
        "<title>%s</title>" % esc((scene.get("meta") or {}).get("title") or "Graphical abstract"),
        "<desc>%s</desc>" % esc("Single-page graphical abstract; editable layered SVG. "
                                "Every element carries data-element/data-evidence."),
    ]
    parts.extend(body)
    parts.append("</svg>")
    data = ("\n".join(parts) + "\n").encode("utf-8")

    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "wb") as fh:
        fh.write(data)

    return {
        "path": path,
        "bytes": len(data),
        "text_nodes": text_nodes,
        "groups": groups,
        "layers": [p.get("layer") for p in prims][:0] + sorted(
            {p.get("layer") for p in prims if p.get("layer")}),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width_mm": W,
        "height_mm": H,
        "family_css": family_css,
        "deterministic": True,
    }

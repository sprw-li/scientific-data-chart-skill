#!/usr/bin/env python3
"""edit.py — source-preserving edits (contract §3/§4).

Owner: validate-export.  Two independent stories:

1. ``apply_edits(spec, edits) -> (spec2, log)``
   Structured, deterministic operations on the *spec* (the editable source of
   truth).  Every change is recorded in the returned log and appended to
   ``spec2["change_records"]`` so that ``result.json`` can carry the audit trail.
   Unknown targets are *skipped with a reason*, never silently altered and never
   fatal — a bad edit request must not destroy the figure.

   Supported ops (aliases in brackets):
     set_label / change_label        {panel, node, label, sublabel?}
     retitle_panel / retitle         {panel, title}
     set_text                        {key: title|subtitle|caption|conclusion|summary, value}
     recolor_series / recolor        {panel, chart, series, color}
     set_node_color                  {panel, node, color}
     set_colorbar                    {panel, vmin?, vmax?, unit?, title?}
     move_panel / move               {panel, to}          (0-based index)
     reorder_panels                  {order: [panel_id, ...]}
     add_node                        {panel, node:{id,label,shape?,source?,sublabel?}}
     remove_node                     {panel, node}
     add_edge                        {panel, edge:{id,from,to,kind?,label?,source?}}
     remove_edge                     {panel, edge}
     set_limitations                 {value: [...]}
   Also accepted: {"op": ..., "target": {...}} as a shorthand for the fields above.

2. ``patch_svg_text(svg_path, replacements) -> dict``
   In-place, structure-preserving rewrite of ``<text>`` node *content* only.
   Element attributes (x/y/font-size/class/data-*), group structure, ids and all
   non-text geometry are left byte-identical, so the figure is NOT re-laid-out.
   Supports wrapped labels: a ``<text>`` whose visible content equals the key is
   rewritten to a single run.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import copy
import hashlib
import os
import re

import scene  # noqa: E402  (frozen shared model; also importable when run as a script)

OPS = (
    "set_label", "change_label", "retitle_panel", "retitle", "set_text",
    "recolor_series", "recolor", "set_node_color", "set_colorbar",
    "move_panel", "move", "reorder_panels", "add_node", "remove_node",
    "add_edge", "remove_edge", "set_limitations",
)

_TEXT_NODE_RE = re.compile(r"<text\b[^>]*>(.*?)</text>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_panel(spec, pid):
    for p in spec.get("panels") or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return p
    return None


def _find_node(panel, nid):
    for n in panel.get("nodes") or []:
        if isinstance(n, dict) and n.get("id") == nid:
            return n
    return None


def _find_chart(panel, cid):
    for i, c in enumerate(panel.get("charts") or []):
        if isinstance(c, dict) and (c.get("id") == cid or c.get("title") == cid):
            return i, c
    return -1, None


def _norm_op(edit):
    op = (edit.get("op") or edit.get("kind") or edit.get("action") or "").strip()
    tgt = edit.get("target") if isinstance(edit.get("target"), dict) else {}
    merged = dict(tgt)
    for k, v in edit.items():
        if k not in ("target", "op", "kind", "action") and k not in merged:
            merged[k] = v
    return op, merged


def _entry(index, op, target, before, after, status, reason=""):
    return {"index": index, "op": op, "target": target, "from": before, "to": after,
            "status": status, "reason": reason}


# --------------------------------------------------------------------------- #
# apply_edits
# --------------------------------------------------------------------------- #
def apply_edits(spec, edits):
    """Apply structured edits to a copy of *spec*.  Returns (spec2, log)."""
    spec2 = copy.deepcopy(spec if isinstance(spec, dict) else {})
    log = []
    if not isinstance(edits, list):
        return spec2, [_entry(0, "?", "?", None, None, "skipped", "edits must be a list")]
    spec2.setdefault("panels", [])
    texts = spec2.setdefault("texts", {})

    for i, raw in enumerate(edits):
        if not isinstance(raw, dict):
            log.append(_entry(i, "?", "?", None, None, "skipped", "edit must be an object"))
            continue
        op, e = _norm_op(raw)
        panel_id = e.get("panel") or e.get("panel_id")

        if op in ("set_label", "change_label"):
            panel = _find_panel(spec2, panel_id)
            if panel is None:
                log.append(_entry(i, op, panel_id, None, None, "skipped", "panel not found"))
                continue
            node = _find_node(panel, e.get("node") or e.get("node_id"))
            if node is None:
                log.append(_entry(i, op, "%s/%s" % (panel_id, e.get("node")), None, None,
                                  "skipped", "node not found"))
                continue
            before = {"label": node.get("label"), "sublabel": node.get("sublabel")}
            if e.get("label") is not None:
                node["label"] = str(e["label"])
            if e.get("sublabel") is not None:
                node["sublabel"] = str(e["sublabel"])
            log.append(_entry(i, op, "%s/%s" % (panel_id, node.get("id")), before,
                              {"label": node.get("label"), "sublabel": node.get("sublabel")},
                              "applied"))

        elif op in ("retitle_panel", "retitle"):
            panel = _find_panel(spec2, panel_id)
            if panel is None or e.get("title") is None:
                log.append(_entry(i, op, panel_id, None, None, "skipped",
                                  "panel not found" if panel is None else "title missing"))
                continue
            before = panel.get("title")
            panel["title"] = str(e["title"])
            log.append(_entry(i, op, panel_id, before, panel.get("title"), "applied"))

        elif op == "set_text":
            key = e.get("key") or e.get("name")
            if key not in ("title", "subtitle", "caption", "conclusion", "summary", "legend_note"):
                log.append(_entry(i, op, key, None, None, "skipped", "unknown text key"))
                continue
            before = texts.get(key)
            texts[key] = str(e.get("value", ""))
            log.append(_entry(i, op, key, before, texts[key], "applied"))

        elif op in ("recolor_series", "recolor"):
            panel = _find_panel(spec2, panel_id)
            if panel is None:
                log.append(_entry(i, op, panel_id, None, None, "skipped", "panel not found"))
                continue
            ci, chart = _find_chart(panel, e.get("chart") or e.get("chart_id"))
            if chart is None:
                log.append(_entry(i, op, "%s/%s" % (panel_id, e.get("chart")), None, None,
                                  "skipped", "chart not found"))
                continue
            name = e.get("series") or e.get("series_name")
            color = e.get("color")
            done = False
            for s in chart.get("series") or []:
                if isinstance(s, dict) and (s.get("name") == name):
                    before = s.get("color")
                    s["color"] = color
                    log.append(_entry(i, op, "%s/%s/%s" % (panel_id, chart.get("id"), name),
                                      before, color, "applied"))
                    done = True
            if not done:
                log.append(_entry(i, op, "%s/%s/%s" % (panel_id, chart.get("id"), name),
                                  None, None, "skipped", "series not found"))
            # keep the panel legend consistent when the series is a legend category
            for leg in [panel.get("legend")] + ([panel.get("legends")] if isinstance(panel.get("legends"), list) else []):
                if isinstance(leg, dict):
                    for it in leg.get("items") or []:
                        if isinstance(it, dict) and it.get("label") == name and it.get("color") != color:
                            it["color"] = color

        elif op == "set_node_color":
            panel = _find_panel(spec2, panel_id)
            node = _find_node(panel, e.get("node")) if panel else None
            if node is None:
                log.append(_entry(i, op, "%s/%s" % (panel_id, e.get("node")), None, None,
                                  "skipped", "panel/node not found"))
                continue
            before = node.get("color")
            node["color"] = e.get("color")
            log.append(_entry(i, op, "%s/%s" % (panel_id, node.get("id")), before, node.get("color"), "applied"))

        elif op == "set_colorbar":
            panel = _find_panel(spec2, panel_id)
            cbs = (panel or {}).get("colorbars") or []
            if not cbs:
                log.append(_entry(i, op, panel_id, None, None, "skipped", "no colorbar in panel"))
                continue
            cb = cbs[0]
            before = {k: cb.get(k) for k in ("vmin", "vmax", "unit", "title")}
            for k in ("vmin", "vmax", "unit", "title"):
                if e.get(k) is not None:
                    cb[k] = e[k]
            log.append(_entry(i, op, "%s/%s" % (panel_id, cb.get("id")), before,
                              {k: cb.get(k) for k in before}, "applied"))

        elif op in ("move_panel", "move"):
            panel = _find_panel(spec2, panel_id)
            if panel is None or not isinstance(e.get("to"), int):
                log.append(_entry(i, op, panel_id, None, None, "skipped",
                                  "panel not found" if panel is None else "'to' must be an int index"))
                continue
            src = spec2["panels"].index(panel)
            to = max(0, min(len(spec2["panels"]) - 1, e["to"]))
            spec2["panels"].pop(src)
            spec2["panels"].insert(to, panel)
            log.append(_entry(i, op, panel_id, src, to, "applied"))

        elif op == "reorder_panels":
            order = e.get("order") or []
            by_id = {p.get("id"): p for p in spec2["panels"] if isinstance(p, dict)}
            new = [by_id[pid] for pid in order if pid in by_id]
            new += [p for p in spec2["panels"] if p not in new]
            before = [p.get("id") for p in spec2["panels"]]
            spec2["panels"] = new
            log.append(_entry(i, op, "panels", before, [p.get("id") for p in new], "applied"))

        elif op == "add_node":
            panel = _find_panel(spec2, panel_id)
            node = e.get("node") if isinstance(e.get("node"), dict) else None
            if panel is None or node is None or not node.get("id") or not node.get("label"):
                log.append(_entry(i, op, panel_id, None, None, "skipped",
                                  "panel or node{id,label} missing"))
                continue
            node = copy.deepcopy(node)
            node.setdefault("shape", "rrect")
            panel.setdefault("nodes", []).append(node)
            log.append(_entry(i, op, "%s/%s" % (panel_id, node.get("id")), None,
                              node.get("label"), "applied"))

        elif op == "remove_node":
            panel = _find_panel(spec2, panel_id)
            nid = e.get("node")
            node = _find_node(panel, nid) if panel else None
            if node is None:
                log.append(_entry(i, op, "%s/%s" % (panel_id, nid), None, None, "skipped", "node not found"))
                continue
            panel["nodes"].remove(node)
            dropped = []
            for ed in list(panel.get("edges") or []):
                if ed.get("from") == nid or ed.get("to") == nid:
                    panel["edges"].remove(ed)
                    dropped.append(ed.get("id"))
            log.append(_entry(i, op, "%s/%s" % (panel_id, nid), node.get("label"), None, "applied",
                              "also removed %d dependent edge(s): %s" % (len(dropped), ",".join(map(str, dropped)))))

        elif op == "add_edge":
            panel = _find_panel(spec2, panel_id)
            edge = e.get("edge") if isinstance(e.get("edge"), dict) else None
            if panel is None or edge is None or not edge.get("id") or not edge.get("from") or not edge.get("to"):
                log.append(_entry(i, op, panel_id, None, None, "skipped", "panel or edge{id,from,to} missing"))
                continue
            panel.setdefault("edges", []).append(copy.deepcopy(edge))
            log.append(_entry(i, op, "%s/%s" % (panel_id, edge.get("id")), None,
                              "%s->%s" % (edge.get("from"), edge.get("to")), "applied"))
            if edge.get("kind") is not None and edge.get("kind") not in scene.EDGE_KINDS:
                log[-1]["status"] = "applied"
                log[-1]["reason"] = "kind %r not in EDGE_KINDS; spec validation will reject it" % (edge.get("kind"),)

        elif op == "remove_edge":
            panel = _find_panel(spec2, panel_id)
            eid = e.get("edge")
            hit = None
            for ed in panel.get("edges") or [] if panel else []:
                if ed.get("id") == eid:
                    hit = ed
                    break
            if hit is None:
                log.append(_entry(i, op, "%s/%s" % (panel_id, eid), None, None, "skipped", "edge not found"))
                continue
            panel["edges"].remove(hit)
            log.append(_entry(i, op, "%s/%s" % (panel_id, eid),
                              "%s->%s" % (hit.get("from"), hit.get("to")), None, "applied"))

        elif op == "set_limitations":
            before = list(spec2.get("limitations") or [])
            spec2["limitations"] = list(e.get("value") or [])
            log.append(_entry(i, op, "limitations", before, spec2["limitations"], "applied"))

        else:
            log.append(_entry(i, op, panel_id, None, None, "skipped",
                              "unknown op %r (supported: %s)" % (op, ",".join(sorted(set(OPS))))))

    records = list(spec2.get("change_records") or [])
    for entry in log:
        if entry["status"] == "applied":
            records.append({"op": entry["op"], "target": entry["target"],
                            "from": entry["from"], "to": entry["to"],
                            "source": "apply_edits"})
    spec2["change_records"] = records
    return spec2, log


# --------------------------------------------------------------------------- #
# patch_svg_text
# --------------------------------------------------------------------------- #
def _plain_text(inner):
    return _TAG_RE.sub("", inner).replace("\n", " ").strip()


def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def patch_svg_text(svg_path, replacements):
    """Rewrite the content of matching ``<text>`` nodes in place.

    ``replacements`` = {old_visible_text: new_text}.  Matching is on the visible
    (tag-stripped, whitespace-trimmed) content of each ``<text>`` node, so wrapped
    labels stored as tspan runs are matched as a whole.  Only the text content
    changes: attributes, ids, group order and every non-text byte are preserved.
    """
    out = {"path": svg_path, "applied": [], "missed": [],
           "text_nodes_before": 0, "text_nodes_after": 0, "groups_before": 0, "groups_after": 0,
           "old_sha256": None, "new_sha256": None, "changed_bytes": False, "parses": False,
           "error": None}
    if not isinstance(replacements, dict) or not replacements:
        out["error"] = "replacements must be a non-empty dict"
        return out
    if not os.path.exists(svg_path):
        out["error"] = "svg not found: %s" % svg_path
        return out
    with open(svg_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    out["old_sha256"] = _sha256_text(src)
    out["groups_before"] = len(re.findall(r"<g\b", src))
    matches = list(_TEXT_NODE_RE.finditer(src))
    out["text_nodes_before"] = len(matches)

    wanted = {str(k): str(v) for k, v in replacements.items()}
    seen = set()
    chunks, last = [], 0
    for m in matches:
        visible = _plain_text(m.group(1))
        if visible in wanted:
            chunks.append(src[last:m.start()])
            chunks.append(m.group(0)[:m.group(0).index(">") + 1] + _xml_escape(wanted[visible]) + "</text>")
            last = m.end()
            seen.add(visible)
            out["applied"].append({"text": visible, "replacement": wanted[visible]})
    chunks.append(src[last:])
    patched = "".join(chunks)
    out["missed"] = sorted(k for k in wanted if k not in seen)
    out["changed_bytes"] = patched != src
    out["text_nodes_after"] = len(re.findall(r"<text\b", patched))
    out["groups_after"] = len(re.findall(r"<g\b", patched))
    out["new_sha256"] = _sha256_text(patched)

    # structure survival: ids and tags must be untouched
    ids_before = re.findall(r'\bid="([^"]*)"', src)
    ids_after = re.findall(r'\bid="([^"]*)"', patched)
    out["ids_preserved"] = ids_before == ids_after
    out["ids_unique_after"] = len(ids_after) == len(set(ids_after))
    out["structure_preserved"] = (out["groups_before"] == out["groups_after"]
                                  and out["text_nodes_before"] == out["text_nodes_after"]
                                  and out["ids_preserved"])

    if out["changed_bytes"]:
        tmp = svg_path + ".tmp-patch"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(patched)
        os.replace(tmp, svg_path)
    # parse the on-disk result (regex-only parse is not enough evidence)
    try:
        import xml.etree.ElementTree as ET
        ET.parse(svg_path)
        out["parses"] = True
    except Exception as exc:
        out["error"] = "patched svg does not parse: %s" % exc
    with open(svg_path, "rb") as fh:
        out["sha256_on_disk"] = hashlib.sha256(fh.read()).hexdigest()
    return out

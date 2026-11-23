#!/usr/bin/env python3
"""datamap.py — the drawn figure -> source data map, in both directions.

Frozen API (CONTRACT §4.3)
--------------------------
    build_map(scene, spec, materials, case=None) -> dict          # §4.1 document
    build_source_map(scene, spec, materials, case=None) -> dict   # element-level sources
    mark_to_source(map_obj, mark_id) -> dict | None               # mark record
    cell_to_marks(map_obj, cell) -> list                          # [mark_id, ...]

The figure's marks are taken from the LAYOUT RESULT, not from the spec, so the
map describes what was actually drawn:

1. ``scene["marks"]`` when the layout engine emits an explicit mark list;
2. otherwise every ``scene["primitives"]`` entry whose ``element`` is a data
   mark (``chart-bar``, ``chart-dot``, ``chart-cell``, ``chart-errorbar``, ...);
3. only then the spec's ``charts[].series``, as an explicitly labelled fallback
   (``marks_source: "spec-fallback"``) for runs where the scene is unavailable.

Numeric provenance rules (§4.1):
* a mark's ``value`` must equal a real material cell within ``TOL`` (1e-9), or
  the mark must be ``derived`` and list every contributing cell in
  ``source_values``;
* every mark cell is registered in the reverse index ``cells``;
* anything that cannot be grounded goes to ``unmapped`` with a reason, and every
  one-to-many match goes to ``ambiguous`` with its candidates.  Nothing is
  invented to make coverage look better.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import datamodel  # noqa: E402

SCHEMA_VERSION = "1.0"
TOL = 1e-9

# element name -> mark family
MARK_FAMILIES = {
    "bar": "bar", "barh": "bar", "column": "bar", "waterfall": "bar", "waterfall-step": "bar",
    "dot": "pt", "point": "pt", "marker": "pt", "scatter": "pt", "symbol": "pt",
    "line-marker": "pt", "vertex": "pt",
    "cell": "cell", "heatcell": "cell", "heatmap-cell": "cell", "heat": "cell",
    "errorbar": "eb", "errorbar-cap": "eb", "err": "eb", "error": "eb",
    "box": "box", "boxplot": "box", "box-whisker": "box", "box-median": "box",
    "violin": "violin", "sector": "sector", "pie-slice": "sector",
    "significance": "sig", "sig-bracket": "sig", "bracket": "sig",
}


def _fam(element):
    if not element:
        return None
    el = str(element).lower()
    if el.startswith("chart-"):
        el = el[len("chart-"):]
    return MARK_FAMILIES.get(el)


# --------------------------------------------------------------------------- #
# 1. collect what was actually drawn
# --------------------------------------------------------------------------- #
def _chart_meta(spec):
    """chart_id -> {"panel_id", "categories", "series": [{name, column, index}], "type"}"""
    out = {}
    for panel in (spec.get("panels") or []):
        for ci, chart in enumerate(panel.get("charts") or []):
            cid = chart.get("id") or scene_id("C", "%s|%d" % (panel.get("id"), ci))
            out[cid] = {
                "panel_id": panel.get("id"),
                "type": chart.get("type"),
                "categories": [str(c) for c in (chart.get("categories") or [])],
                "unit": chart.get("unit"),
                "series": [
                    {"name": s.get("name"), "column": s.get("column"), "index": si,
                     "values": s.get("values"), "errors": s.get("errors"),
                     "derived": bool(s.get("derived")), "source_values": s.get("source_values")}
                    for si, s in enumerate(chart.get("series") or [])
                ],
            }
    return out


def scene_id(prefix, seed):
    import hashlib
    return "%s%s" % (prefix, hashlib.sha1(("%s|%s" % (prefix, seed)).encode("utf-8")).hexdigest()[:8])


def _from_scene_marks(scene, spec):
    """Layout-engine mark list (preferred when present)."""
    meta = _chart_meta(spec)
    marks = []
    for i, m in enumerate(scene.get("marks") or []):
        if not isinstance(m, dict):
            continue
        cid = m.get("chart_id")
        si = m.get("series_index", m.get("series"))
        if isinstance(si, str):
            si = _series_index(meta.get(cid), si)
        si = int(si) if isinstance(si, (int, float)) else 0
        fam = m.get("mark_kind") or _fam(m.get("element") or m.get("kind")) or "pt"
        idx = m.get("index", i)
        mid = m.get("mark_id") or m.get("id") or "%s:%d#%s:%s" % (
            "series", si, fam, _fmt_index(fam, idx, m))
        marks.append({
            "mark_id": mid,
            "chart_id": cid,
            "panel_id": m.get("panel_id") or m.get("panel") or (meta.get(cid) or {}).get("panel_id"),
            "series": m.get("series_name") or m.get("series") if isinstance(m.get("series"), str) else m.get("series_name"),
            "category": m.get("category"),
            "value": m.get("value"),
            "family": fam,
            "column": m.get("column"),
            "row_index": m.get("row_index"), "col_index": m.get("col_index"),
            "derived": bool(m.get("derived")),
            "source_values": m.get("source_values"),
            "error_kind": m.get("error_kind"),
            "element": m.get("element") or m.get("kind"),
            "primitive_id": m.get("primitive_id") or m.get("element_id"),
            "drawn": True,
        })
    return marks


def _fmt_index(fam, idx, m):
    if fam == "cell":
        r = m.get("row_index")
        c = m.get("col_index")
        if r is not None and c is not None:
            return "%s,%s" % (r, c)
        return str(idx)
    return str(idx)


def _series_index(cmeta, name):
    if not cmeta:
        return 0
    for s in cmeta["series"]:
        if s["name"] == name:
            return s["index"]
    return 0


def _from_scene_primitives(scene, spec):
    """Every drawn primitive whose element is a data mark."""
    meta = _chart_meta(spec)
    marks = []
    counters = {}
    for pr in (scene.get("primitives") or []):
        if not isinstance(pr, dict):
            continue
        fam = _fam(pr.get("element"))
        if fam is None:
            continue
        cid = pr.get("chart_id")
        si = pr.get("series_index")
        if not isinstance(si, int):
            si = _series_index(meta.get(cid), pr.get("series_name"))
        cmeta = meta.get(cid) or {}
        key = (cid, si, fam)
        idx = counters.get(key, 0)
        counters[key] = idx + 1
        row_index = col_index = None
        if fam == "cell":
            cats = cmeta.get("categories") or []
            cat = pr.get("category")
            if cat in cats:
                row_index = cats.index(cat)
            elif isinstance(pr.get("row_index"), int):
                row_index = pr["row_index"]
            col_index = si
            idx_repr = "%s,%s" % (row_index if row_index is not None else idx, col_index)
        else:
            idx_repr = str(idx)
        series_name = pr.get("series_name")
        if series_name is None and cmeta.get("series") and si < len(cmeta["series"]):
            series_name = cmeta["series"][si]["name"]
        marks.append({
            "mark_id": "series:%d#%s:%s" % (si, fam, idx_repr),
            "chart_id": cid,
            "panel_id": pr.get("panel"),
            "series": series_name,
            "category": pr.get("category"),
            "value": pr.get("value"),
            "family": fam,
            "column": (cmeta.get("series") or [{}])[si].get("column") if si < len(cmeta.get("series") or [{}]) else None,
            "row_index": row_index, "col_index": col_index,
            "derived": bool((cmeta.get("series") or [{}])[si].get("derived")) if si < len(cmeta.get("series") or [{}]) else False,
            "source_values": (cmeta.get("series") or [{}])[si].get("source_values") if si < len(cmeta.get("series") or [{}]) else None,
            "error_kind": None,
            "element": pr.get("element"),
            "primitive_id": pr.get("id"),
            "drawn": True,
        })
    return marks


def _from_spec(spec):
    """Fallback: the marks the spec asks for (used only when no scene marks exist).

    Labelled ``marks_source = "spec-fallback"`` so a reviewer can tell an
    "actually drawn" map from an "intended" one.
    """
    out = []
    for panel in (spec.get("panels") or []):
        for chart in (panel.get("charts") or []):
            cid = chart.get("id")
            ctype = chart.get("type") or "bar"
            families = {"bar": "bar", "barh": "bar", "waterfall": "bar", "line": "pt",
                        "scatter": "pt", "dot": "pt", "heatmap": "cell", "box": "box",
                        "violin": "violin"}.get(ctype, "pt")
            for si, s in enumerate(chart.get("series") or []):
                vals = s.get("values") or []
                for i, v in enumerate(vals):
                    if families == "cell":
                        rep = "%d,%d" % (i, si)
                    else:
                        rep = str(i)
                    out.append({
                        "mark_id": "series:%d#%s:%s" % (si, families, rep),
                        "chart_id": cid, "panel_id": panel.get("id"),
                        "series": s.get("name"), "category": _cat_of(chart, i),
                        "value": v, "family": families, "column": s.get("column"),
                        "row_index": i if families == "cell" else None,
                        "col_index": si if families == "cell" else None,
                        "derived": bool(s.get("derived")), "source_values": s.get("source_values"),
                        "error_kind": None, "element": "spec-series",
                        "primitive_id": None, "drawn": False,
                    })
                if s.get("errors"):
                    for i, e in enumerate(s["errors"]):
                        out.append({
                            "mark_id": "series:%d#eb:%d" % (si, i),
                            "chart_id": cid, "panel_id": panel.get("id"),
                            "series": s.get("name"), "category": _cat_of(chart, i),
                            "value": e, "family": "eb", "column": None,
                            "row_index": None, "col_index": None,
                            "derived": True, "source_values": s.get("source_values"),
                            "error_kind": s.get("error_kind"), "element": "spec-errorbar",
                            "primitive_id": None, "drawn": False,
                        })
            for k, sig in enumerate(chart.get("significance") or []):
                p = sig.get("p")
                out.append({
                    "mark_id": "sig:%d" % k, "chart_id": cid, "panel_id": panel.get("id"),
                    "series": None, "category": None,
                    "value": p if isinstance(p, (int, float)) else None,
                    "family": "sig", "column": None, "row_index": None, "col_index": None,
                    "derived": False, "source_values": None, "error_kind": None,
                    "element": "significance-bracket", "primitive_id": None,
                    "drawn": False, "significance": sig,
                })
    return out


def collect_marks(scene, spec):
    """-> (marks, marks_source). Prefers what was drawn."""
    if isinstance(scene, dict) and scene.get("marks"):
        return _from_scene_marks(scene, spec), "scene-marks"
    if isinstance(scene, dict) and scene.get("primitives"):
        found = _from_scene_primitives(scene, spec)
        if found:
            return found, "scene-primitives"
    return _from_spec(spec), "spec-fallback"


# --------------------------------------------------------------------------- #
# 2. match a mark against the real table cells
# --------------------------------------------------------------------------- #
class CellIndex(object):
    def __init__(self, dt):
        self.dt = dt
        self.tables = list(datamodel.iter_tables(dt))
        self.cells = list(datamodel.iter_cells(dt))
        self._group_cache = {}

    # -- helpers ---------------------------------------------------------- #
    def grouping_column(self, table, values):
        """The narrowest column whose value set contains every label in ``values``.

        A grouping column is identified by CONTENT, not by name: the column that
        holds all of a chart's category labels (or all of its series labels) is
        the column the chart was grouped by.  ``None`` when no single column
        holds them all -- the caller must then not filter on it.
        """
        want = [datamodel.norm_label(v) for v in values if v is not None and str(v) != ""]
        if not want:
            return None
        key = (id(table), tuple(sorted(set(want))))
        if key in self._group_cache:
            return self._group_cache[key]
        cols = table.get("columns") or []
        found = None
        for ci in range(len(cols)):
            seen = set()
            for r in (table.get("rows") or []):
                cells = r.get("cells") or []
                if ci < len(cells):
                    seen.add(datamodel.norm_label(cells[ci]))
            if all(w in seen for w in want):
                found = ci
                break
        self._group_cache[key] = found
        return found

    def measures_column(self, table, hints):
        for h in hints:
            if not h:
                continue
            ci = datamodel.column_index(table, h)
            if ci is not None:
                return ci
        return None

    def filters_for(self, table, mark, cmeta):
        """[(column_index, expected_label), ...] that select this mark's group."""
        pairs = {}
        cats = [c for c in (cmeta.get("categories") or []) if str(c) != ""]
        if mark.get("category") is not None and cats:
            ci = self.grouping_column(table, cats)
            if ci is not None and datamodel.norm_label(mark["category"]) in {
                    datamodel.norm_label(c) for c in cats}:
                pairs.setdefault(ci, mark["category"])
        names = [s["name"] for s in (cmeta.get("series") or []) if s.get("name")]
        if mark.get("series") and names:
            ci = self.grouping_column(table, names)
            if ci is not None:
                pairs.setdefault(ci, mark["series"])
        return list(pairs.items())

    def _records(self, table, row, column, ci, value, raw, start=None, end=None):
        return {
            "file": table.get("file"), "table": table.get("name"), "row": row,
            "column": column, "index": ci, "raw": raw, "value": value,
            "cell": datamodel.cell_key(table.get("file"), table.get("name"), row, column),
            "start": start, "end": end,
        }

    def _rows_match(self, table, row, filters):
        cells = row.get("cells") or []
        for ci, want in filters:
            raw = cells[ci] if ci < len(cells) else ""
            if datamodel.norm_label(raw) != datamodel.norm_label(want):
                return False
        return True

    # -- the actual search ------------------------------------------------ #
    def locate_value(self, value, col_hints=(), mark=None, cmeta=None,
                     prefer_column=None, row_hint=None):
        """All cells whose numeric value equals ``value`` within TOL.

        Column-aware first (the measure column the series names), then group
        filters (category x series labels found by content).  Only when neither
        is known does it fall back to scanning every numeric column.
        """
        if value is None:
            return []
        hits = []
        for t in self.tables:
            cols = t.get("columns") or []
            ci = self.measures_column(t, list(col_hints))
            filters = self.filters_for(t, mark, cmeta) if mark else []
            scan = [ci] if ci is not None else datamodel.numeric_columns(t)
            for r in (t.get("rows") or []):
                cells = r.get("cells") or []
                if row_hint is not None and int(r.get("row")) != int(row_hint):
                    continue
                if filters and not self._rows_match(t, r, filters):
                    continue
                for cj in scan:
                    if cj is None or cj >= len(cells):
                        continue
                    v = datamodel.parse_number(cells[cj])
                    if v is None or abs(v - value) > TOL:
                        continue
                    hits.append(self._records(t, r.get("row"), cols[cj], cj, v, cells[cj],
                                              r.get("start"), r.get("end")))
        return _rank(hits, prefer_column)

    def group_cells(self, mark, cmeta, col_hints):
        """Every cell of the group a derived mark aggregates -> [(record, value), ...]."""
        out = []
        for t in self.tables:
            cols = t.get("columns") or []
            ci = self.measures_column(t, list(col_hints))
            if ci is None:
                continue
            filters = self.filters_for(t, mark, cmeta)
            group = []
            for r in (t.get("rows") or []):
                cells = r.get("cells") or []
                if filters and not self._rows_match(t, r, filters):
                    continue
                if ci >= len(cells):
                    continue
                v = datamodel.parse_number(cells[ci])
                if v is None:
                    continue
                group.append((self._records(t, r.get("row"), cols[ci], ci, v, cells[ci],
                                            r.get("start"), r.get("end")), v))
            if group:
                return group
        return out


def _rank(hits, prefer_column):
    """Deterministic ordering; exact column matches win, then first file/row."""
    def key(rec):
        exact = 0 if (prefer_column and rec["column"] == prefer_column) else 1
        loose = 0 if (prefer_column and datamodel.norm_label(prefer_column) ==
                      datamodel.norm_label(rec["column"])) else 1
        return (exact, loose, str(rec["file"]), str(rec["table"]), int(rec["row"] or 0), rec["index"])
    return sorted(hits, key=key)


def _value_hints(mark, cmeta):
    """Column hints for a mark, best first: explicit column, then series name."""
    out = []
    si = None
    if cmeta:
        for s in cmeta["series"]:
            if s["name"] == mark.get("series"):
                si = s["index"]
                break
    if mark.get("column"):
        out.append(mark["column"])
    if cmeta and si is not None:
        s = cmeta["series"][si]
        if s.get("column"):
            out.append(s["column"])
        out.append(s.get("name"))
    if mark.get("series"):
        out.append(mark["series"])
    return out


def _error_hints(mark, cmeta):
    """For an error bar, prefer an explicit error column in the table."""
    out = []
    for h in _value_hints(mark, cmeta):
        if not h:
            continue
        out.extend(["%s_sd" % h, "%s_sem" % h, "sd", "standard deviation", "sem", "stderr",
                    "ci", "error", "%s_err" % h])
    return out


def ground_mark(mark, index, cmeta):
    """-> (source_dict|None, derived_info|None, unmapped_reason|None, ambiguous|None)"""
    family = mark.get("family") or "pt"
    value = mark.get("value")
    if family == "sig":
        return None, None, "significance marker carries no numeric value in a source cell", None
    if value is None:
        return None, None, "mark has no numeric value to trace", None

    hints = _value_hints(mark, cmeta)
    prefer = next((h for h in hints if h), None)

    if family == "eb":
        hits = index.locate_value(value, _error_hints(mark, cmeta), mark, cmeta, None)
        if hits:
            return _src(hits[0]), None, None, _ambiguity(mark, hits)
        # error bars are normally COMPUTED (e.g. SD of a group): that is derived
        sv = index.group_cells(mark, cmeta, _value_hints(mark, cmeta))
        if sv:
            agg = _aggregate_check(mark, sv, kind="error")
            return _src(sv[0][0]), {"aggregate": "error", "source_values": sv,
                                    "check": agg}, None, _aggregate_ambiguity(mark, agg, sv)
        return None, None, ("error magnitude %r is neither a cell value nor recomputable from "
                            "the group it belongs to" % (value,)), None

    if mark.get("derived"):
        sv = index.group_cells(mark, cmeta, hints)
        if not sv:
            return None, None, ("mark is marked derived but the group it aggregates could not be "
                                "located in the tables"), None
        agg = _aggregate_check(mark, sv, kind=mark.get("aggregate") or "mean")
        return (_src(sv[0][0]), {"aggregate": mark.get("aggregate") or "mean",
                                 "source_values": sv, "check": agg},
                None, _aggregate_ambiguity(mark, agg, sv))

    hits = index.locate_value(value, hints, mark, cmeta, prefer)
    if hits:
        return _src(hits[0]), None, None, _ambiguity(mark, hits)
    # last resort: the value occurs in the material but outside this group.
    # That is WEAK provenance and is disclosed as such.
    loose = index.locate_value(value, hints, None, None, prefer)
    if loose:
        return _src(loose[0]), None, None, {
            "mark_id": mark["mark_id"], "value": value, "kind": "group-mismatch",
            "chosen": loose[0]["cell"], "candidates": [c["cell"] for c in loose[:12]],
            "note": "value occurs in the material but not inside the group this mark belongs to"}
    return None, None, "value %r does not occur in any material cell (within 1e-9)" % (value,), None


def _aggregate_check(mark, sv, kind):
    """Recompute the aggregate from the located raw cells; report the delta."""
    vals = [v for _rec, v in sv]
    n = len(vals)
    if n == 0:
        return {"kind": kind, "n": 0, "recomputed": None, "delta": None}
    if kind == "mean":
        stat = sum(vals) / float(n)
    elif kind == "sum":
        stat = sum(vals)
    elif kind in ("sd", "error"):
        if n < 2:
            stat = 0.0
        else:
            mu = sum(vals) / float(n)
            stat = (sum((x - mu) ** 2 for x in vals) / float(n - 1)) ** 0.5
    else:
        stat = sum(vals) / float(n)
    try:
        delta = abs(float(stat) - float(mark.get("value")))
    except (TypeError, ValueError):
        delta = None
    return {"kind": kind, "n": n, "recomputed": stat, "delta": delta,
            "matches": bool(delta is not None and delta <= max(TOL, 1e-6 * abs(float(mark.get("value") or 0.0))))}


def _aggregate_ambiguity(mark, agg, sv):
    if agg.get("matches"):
        return None
    return {
        "mark_id": mark["mark_id"], "value": mark.get("value"), "kind": "aggregate-mismatch",
        "chosen": sv[0][0]["cell"], "candidates": [r["cell"] for r, _v in sv[:12]],
        "note": "the %s of the located raw cells is %r (delta %r), which does not reproduce the "
                "drawn value; the raw cells are disclosed but the aggregate is NOT recomputable"
                % (agg.get("kind"), agg.get("recomputed"), agg.get("delta")),
    }


def _ambiguity(mark, hits):
    keys = []
    for h in hits:
        if h["cell"] not in keys:
            keys.append(h["cell"])
    if len(keys) <= 1:
        return None
    return {
        "mark_id": mark["mark_id"], "value": mark.get("value"), "kind": "duplicate-value",
        "chosen": hits[0]["cell"], "candidates": keys[:12],
        "note": "%d cells hold this value; the column/group-aware ranking picked the first in "
                "file/table/row order" % len(keys),
    }


def _src(rec):
    return {
        "file": rec["file"], "table": rec["table"], "row": rec["row"],
        "column": rec["column"], "cell": rec["cell"],
        "row_label": rec.get("row_label"),
        "quote": rec.get("raw"),
        "row_span": [rec["start"], rec["end"]] if rec.get("start") is not None else None,
    }


# --------------------------------------------------------------------------- #
# 3. the two documents
# --------------------------------------------------------------------------- #
def _materials_block(dt):
    out = []
    for f in (dt.get("files") or []):
        if not f.get("n_tables"):
            continue
        out.append({
            "file": f.get("file"), "sha256": f.get("sha256"), "format": f.get("format"),
            "tables": [{"name": t.get("name"), "header_row": t.get("header_row"),
                        "n_rows": t.get("n_rows"), "columns": list(t.get("columns") or [])}
                       for t in (dt.get("tables") or []) if t.get("file") == f.get("file")],
        })
    return out


def build_map(scene, spec, materials, case=None):
    """-> the ``mark_data_map.json`` document (CONTRACT §4.1)."""
    materials = materials or {}
    dt = datamodel.build_tables(materials, materials.get("input_dir"))
    index = CellIndex(dt)
    cmeta_all = _chart_meta(spec)
    marks_in, marks_source = collect_marks(scene or {}, spec)
    if not marks_source:
        marks_source = "none"

    marks_out, cells_index, unmapped, ambiguous = [], {}, [], []
    for mark in marks_in:
        cmeta = cmeta_all.get(mark.get("chart_id")) or {}
        src, derived_info, reason, amb = ground_mark(mark, index, cmeta)
        if amb:
            ambiguous.append(amb)
        if src is None and mark.get("family") == "sig":
            # a significance marker with no p-value is a REPORTED RELATION from
            # prose: locate it by evidence span, never by inventing a number.
            span = _sig_source(mark, spec, materials)
            if span:
                src = span
                reason = None
        entry = {
            "mark_id": mark["mark_id"],
            "chart_id": mark.get("chart_id"),
            "panel_id": mark.get("panel_id"),
            "series": mark.get("series"),
            "category": mark.get("category"),
            "value": mark.get("value"),
            "source": src,
        }
        if src is None:
            unmapped.append({
                "mark_id": mark["mark_id"], "chart_id": mark.get("chart_id"),
                "series": mark.get("series"), "category": mark.get("category"),
                "value": mark.get("value"), "reason": reason,
            })
        else:
            if derived_info:
                entry["derived"] = True
                entry["aggregate"] = derived_info.get("aggregate")
                entry["source_values"] = [
                    {"value": v, "cell": rec["cell"], "row": rec["row"], "column": rec["column"]}
                    for rec, v in derived_info.get("source_values") or []
                ]
            if mark.get("family") == "eb":
                entry["kind"] = "errorbar"
                if mark.get("error_kind"):
                    entry["error_kind"] = mark["error_kind"]
            elif mark.get("family") == "sig":
                entry["kind"] = "significance"
                entry["significance"] = mark.get("significance")
            if src.get("cell") is None and src.get("source_basis") == "evidence-span":
                entry["source_basis"] = "evidence-span"
            _index_cell(cells_index, src.get("cell"), mark["mark_id"])
            for sv in entry.get("source_values") or []:
                _index_cell(cells_index, sv.get("cell"), mark["mark_id"])
        marks_out.append(entry)

    cells_out = {k: sorted(set(v)) for k, v in sorted(cells_index.items())}
    doc = {
        "schema_version": SCHEMA_VERSION,
        "case_id": (materials.get("case_id") or (case or {}).get("case_id")
                    or (spec.get("meta") or {}).get("case_id") or ""),
        "marks_source": marks_source,
        "materials": _materials_block(dt),
        "marks": marks_out,
        "cells": cells_out,
        "unmapped": unmapped,
        "ambiguous": ambiguous,
        "counts": {
            "marks": len(marks_out),
            "mapped": len([m for m in marks_out if m.get("source")]),
            "derived": len([m for m in marks_out if m.get("derived")]),
            "unmapped": len(unmapped),
            "ambiguous": len(ambiguous),
            "cells_indexed": len(cells_out),
            "tables": len(dt.get("tables") or []),
            "cells_scanned": len(index.cells),
            "reverse_index_complete": all(
                m.get("source", {}).get("cell") in cells_out
                for m in marks_out if (m.get("source") or {}).get("cell")),
        },
    }
    return doc


def _index_cell(cells_index, cell, mark_id):
    if not cell:
        return
    cells_index.setdefault(cell, [])
    if mark_id not in cells_index[cell]:
        cells_index[cell].append(mark_id)


def _sig_source(mark, spec, materials):
    """Locate a significance marker in an evidence span (prose), not in a table."""
    sig = mark.get("significance") or {}
    needles = [str(x) for x in (sig.get("between") or []) if x]
    needles.append(str(sig.get("label") or ""))
    text_files = [m for m in (materials.get("materials") or []) if (m.get("text") or "").strip()]
    for m in text_files:
        text = m.get("text") or ""
        for needle in needles:
            if not needle:
                continue
            pos = text.find(needle)
            if pos < 0:
                continue
            start = text.rfind("\n", 0, pos) + 1
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
            line_no = text.count("\n", 0, start) + 1
            return {
                "file": m.get("path"), "table": None, "row": None, "column": None,
                "cell": None, "locator": "line %d" % line_no,
                "quote": text[start:end][:320], "source_basis": "evidence-span",
            }
    return None


def build_source_map(scene, spec, materials, case=None):
    """-> ``source_map.json``: element-level source of every drawn element."""
    elements = []
    seen = set()
    for pr in ((scene or {}).get("primitives") or []):
        if not isinstance(pr, dict):
            continue
        eid = pr.get("id")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        src = pr.get("source")
        element = pr.get("element") or pr.get("type")
        entry = {
            "element_id": eid,
            "kind": element,
            "layer": pr.get("layer"),
            "panel_id": pr.get("panel"),
            "source": src if src else None,
            "evidence_ids": pr.get("evidence_ids") or ([pr["evidence"]] if pr.get("evidence") else []),
            "value": pr.get("value"),
            "text": pr.get("text"),
        }
        if pr.get("chart_id"):
            entry["chart_id"] = pr["chart_id"]
        if pr.get("series_name") is not None:
            entry["series"] = pr["series_name"]
        if pr.get("category") is not None:
            entry["category"] = pr["category"]
        elements.append(entry)
    # elements the layout did not tag with a primitive but the map knows about
    doc = {
        "schema_version": SCHEMA_VERSION,
        "case_id": (materials.get("case_id") or (spec.get("meta") or {}).get("case_id") or ""),
        "universe": "every primitive in the built scene (contract §2.1 element-level source)",
        "elements": elements,
        "counts": {
            "elements": len(elements),
            "with_source": len([e for e in elements if e.get("source")]),
            "without_source": len([e for e in elements if not e.get("source")]),
        },
    }
    return doc


# --------------------------------------------------------------------------- #
# 4. the two queries (CONTRACT §4.2/§4.3)
# --------------------------------------------------------------------------- #
def mark_to_source(map_obj, mark_id):
    """-> the mark record for ``mark_id``, or None. Tolerant of short ids."""
    if not isinstance(map_obj, dict) or not mark_id:
        return None
    want = str(mark_id)
    marks = map_obj.get("marks") or []
    for m in marks:
        if m.get("mark_id") == want:
            return m
    # tolerate "C1:series:0#pt:3" / "series:0#pt:3" forms
    for m in marks:
        mid = str(m.get("mark_id") or "")
        if mid.endswith(":" + want) or mid == want.split(":", 1)[-1]:
            return m
    return None


def cell_to_marks(map_obj, cell):
    """-> [mark_id, ...] for a cell key; tolerant of a bare basename suffix."""
    if not isinstance(map_obj, dict) or not cell:
        return []
    want = datamodel.norm_path(cell).strip()
    cells = map_obj.get("cells") or {}
    if want in cells:
        return list(cells[want])
    hits = []
    for key, ids in cells.items():
        if key == want or key.endswith(want) or key.endswith("#" + want.split("#")[-1]):
            hits.extend(ids or [])
    out = []
    for h in hits:
        if h not in out:
            out.append(h)
    return out


# --------------------------------------------------------------------------- #
# diagnostics CLI (the real pipeline calls the functions above)
# --------------------------------------------------------------------------- #
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="datamap.py", description="build mark_data_map.json from a run")
    p.add_argument("--out", required=True, help="run output dir (needs scene.json + figure_spec.resolved.json)")
    p.add_argument("--work", help="work dir with materials.json")
    p.add_argument("--query-mark")
    p.add_argument("--query-cell")
    args = p.parse_args(argv)

    def load(name, d):
        path = os.path.join(d, name) if d else None
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as fh:
                return json.load(fh)
        return {}

    scene = load("scene.json", args.out)
    spec = load("figure_spec.resolved.json", args.out)
    materials = load("materials.json", args.work)
    case = load("case.json", (args.work or args.out))
    mp = build_map(scene, spec, materials, case)
    print(json.dumps(mp["counts"], ensure_ascii=False, indent=2))
    if args.query_mark:
        hit = mark_to_source(mp, args.query_mark)
        print(json.dumps(hit, ensure_ascii=False, indent=2))
        return 0 if hit else 5
    if args.query_cell:
        hits = cell_to_marks(mp, args.query_cell)
        print(json.dumps(hits, ensure_ascii=False))
        return 0 if hits else 5
    return 0


if __name__ == "__main__":
    sys.exit(main())

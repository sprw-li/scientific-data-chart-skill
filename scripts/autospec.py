#!/usr/bin/env python3
"""autospec.py — derive a figure spec from the material WITHOUT an LLM.

Why this exists
---------------
The Skill's normal path is: an agent reads the material and authors ``spec.json``
(science), then the deterministic scripts draw it.  If the agent is unavailable,
runs out of budget, or the case is a stress case, this module still has to
produce something honest and usable — so the pipeline degrades instead of dying.

Guarantees:
    * deterministic: same materials + ledger -> byte-identical spec
      (no randomness, no clock, no hash-order dependence);
    * valid: the returned spec passes ``scene.validate_spec`` with zero errors
      (a repair pass removes anything it cannot substantiate);
    * honest: every node/edge/series cites an evidence id whose quote really
      occurs in a material, and anything it could not extract becomes an entry
      in ``uncertainty``/``limitations`` instead of an invented value.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import scene            # noqa: E402
import evidence as ev   # noqa: E402

MAX_NODES_PER_PANEL = {"problem": 4, "method": 6, "results": 9, "conclusion": 5}
MAX_CHARTS = 1                 # one chart per panel: two charts would union their
#                              series names into a legend of >4 colours, and beyond
#                              four categories the Okabe-Ito set cannot also clear
#                              the 5.0 greyscale dL* floor (see scene.PALETTES).
MAX_SERIES = 3
MAX_EDGES_PER_PANEL = 6
# A comfortable single page. The default 180x120 double-column canvas is too
# tight for an auto-derived dense figure, and the layout engine must never be
# asked to place content outside the margin box.
AUTOSPEC_CANVAS = {"w_mm": 200.0, "h_mm": 170.0, "margin_mm": 3.0, "dpi": 300}

ROLE_KEYWORDS = {
    "problem": ("background", "intro", "rationale", "problem", "question", "motivation",
                "hypothesis", "aim", "objective", "abstract", "\u80cc\u666f", "\u95ee\u9898"),
    "method": ("method", "material", "experimental", "approach", "protocol", "procedure",
               "workflow", "assay", "\u65b9\u6cd5", "\u5b9e\u9a8c"),
    "results": ("result", "finding", "observation", "measurement", "outcome",
                "\u7ed3\u679c"),
    "conclusion": ("conclusion", "discussion", "summary", "outlook", "perspective",
                   "implication", "legend", "\u7ed3\u8bba", "\u8ba8\u8bba"),
}
ROLE_ORDER = ("problem", "method", "results", "conclusion")

UNIT_HINTS = (
    ("fold", "fold"), ("pct", "percent"), ("percent", "percent"),
    ("pmol", "pmol/min"), ("nmol_per_mg", "nmol/mg"), ("nmol", "nmol/mg"),
    ("mph", "mpH/min"), ("mmhg", "mmHg"), ("umol", "umol/L"), ("mM", "mM"),
    ("activity", "per second"), ("per_s", "per second"),
)


# --------------------------------------------------------------------------- #
def _cjk_ratio(text):
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / float(max(1, len(text)))


def _clean_label(text, limit=34):
    """Short, whole-word label.

    Never cut inside a word: a hard mid-word cut produces a token the layout
    engine cannot wrap ("cardiomyoblas"), which it then drops, and a dropped
    token is a gate B/F failure. If the first word alone is too long, keep it
    (the layout can shrink) rather than mangling it.
    """
    t = ev._norm(text)
    t = re.sub(r"^[#*\-\s]+", "", t)
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", t)
    if len(t) <= limit:
        return t or "unnamed"
    words = t.split(" ")
    kept = []
    for w in words:
        cand = " ".join(kept + [w])
        if len(cand) > limit:
            break
        kept.append(w)
    if kept:
        return " ".join(kept).rstrip(" ,;:")
    return words[0] if words else (t or "unnamed")


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]


_UNIT_WORDS = {
    "fold", "folds", "pmol", "nmol", "umol", "mmol", "mol", "min", "mins", "minute",
    "minutes", "sec", "s", "h", "hr", "hrs", "hour", "hours", "mg", "ml", "l", "kg",
    "mph", "ph", "percent", "pct", "au", "arb", "units", "unit", "per", "of",
}


def header_info(header):
    """CSV header -> (human label, canonical unit).

    The unit is carried by the chart's ``unit`` field, so it is stripped from the
    label: a series called "MitoSOX fold" both duplicates the axis unit and gets
    truncated in a narrow legend, which loses a required label (gate B/F).
    """
    h = str(header or "").strip()
    low = h.lower()
    label = h.replace("_", " ").replace("-", " ").strip()
    label = re.sub(r"\s+", " ", label)
    unit = None
    for token, u in UNIT_HINTS:
        if token.lower() in low:
            unit = u
            break
    if unit is None:
        if low in ("time_h", "time", "hours") or low.endswith("_h"):
            unit = "h"
        elif "viability" in low:
            unit = "percent"
        else:
            unit = "AU"
    words = [w for w in label.split(" ") if w]
    while words and words[-1].lower() in _UNIT_WORDS:
        words.pop()
    trimmed = " ".join(words)
    if len(trimmed) >= 3:
        label = trimmed
    return _clean_label(label, 30), unit


def _numeric(cell):
    try:
        return float(str(cell).strip())
    except (TypeError, ValueError):
        return None


def _column_numbers(table, index):
    out = []
    for row in table.get("rows") or []:
        cells = row.get("cells") or []
        if index < len(cells):
            out.append(_numeric(cells[index]))
    return out


# --------------------------------------------------------------------------- #
# tables -> charts
# --------------------------------------------------------------------------- #
def charts_from_tables(materials, table_evidence):
    """-> (charts, used_evidence_ids, notes). Charts never mix units."""
    tables = []
    for mat in (materials.get("materials") or []):
        for table in (mat.get("tables") or []):
            if table.get("rows"):
                tables.append((mat, table))
    if not tables:
        return [], [], ["no tabular source data found; charts were derived from inline numbers only"]

    notes = []
    mat, table = tables[0]
    header = [str(h) for h in (table.get("header") or [])]
    rows = table.get("rows") or []
    if len(header) < 2 or len(rows) < 2:
        return [], [], ["table %s has too few rows/columns for a chart" % table.get("name")]

    numeric_idx, cat_idx = [], None
    for i in range(len(header)):
        vals = _column_numbers(table, i)
        good = [v for v in vals if v is not None]
        if vals and len(good) >= max(2, int(0.8 * len(vals))):
            numeric_idx.append(i)
        elif cat_idx is None:
            cat_idx = i
    if not numeric_idx:
        return [], [], ["table %s has no numeric column" % table.get("name")]

    # categories: last row per distinct label (the endpoint of a time course)
    cats, cat_rows = [], []
    if cat_idx is not None:
        seen = {}
        for row in rows:
            cells = row.get("cells") or []
            key = str(cells[cat_idx]).strip() if cat_idx < len(cells) else ""
            if key == "":
                continue
            if key not in seen:
                seen[key] = len(cats)
                cats.append(_clean_label(key, 24))
                cat_rows.append(row)
            else:
                cat_rows[seen[key]] = row
    if not cats:
        cats = ["value"]
        cat_rows = [rows[-1]]

    # evidence for the rows actually plotted
    row_ev = {}
    for item in table_evidence:
        row_ev[item.get("row")] = item["id"]

    def ev_for(row):
        return row_ev.get(row.get("n")) or (table_evidence[0]["id"] if table_evidence else None)

    groups = {}
    for i in numeric_idx:
        label, unit = header_info(header[i])
        vals = []
        for row in cat_rows:
            cells = row.get("cells") or []
            vals.append(_numeric(cells[i]) if i < len(cells) else None)
        if any(v is None for v in vals):
            continue
        groups.setdefault(unit, []).append((i, label, vals))

    if not groups:
        return [], [], ["table %s yielded no complete numeric series" % table.get("name")]

    def spread(entry):
        vals = entry[2]
        lo, hi = min(vals), max(vals)
        return abs(hi - lo) / max(1e-9, abs(hi), abs(lo))

    charts, used = [], []
    # deterministic group order: unit groups sorted by their best spread
    ordered_units = sorted(groups, key=lambda u: (-max(spread(e) for e in groups[u]), u))
    for unit in ordered_units[:MAX_CHARTS]:
        entries = sorted(groups[unit], key=lambda e: (-spread(e), e[0]))[:MAX_SERIES]
        # every plotted row must be cited, otherwise the chart would show numbers
        # whose own source span is not referenced (gate E906)
        row_sources = []
        for row in cat_rows:
            sid = ev_for(row)
            if sid and sid not in row_sources:
                row_sources.append(sid)
        series = []
        for i, label, vals in entries:
            series.append({"name": label, "values": [round(v, 6) for v in vals],
                           "source": row_sources[0] if len(row_sources) == 1 else None,
                           "sources": row_sources})
        used.extend(row_sources)
        charts.append({
            "id": scene.stable_id("C", "%s|%s" % (table.get("name"), unit)),
            "type": "bar",
            # keep the title short enough to render without truncation: the
            # category axis already says what the grouping is
            "title": _clean_label(header[entries[0][0]], 30),
            "unit": unit,
            "categories": cats,
            "series": series,
        })
    notes.append("charts built from %s (%d row(s) x %d column(s))"
                 % (table.get("name"), len(rows), len(header)))
    return charts, used, notes


def charts_from_numbers(items):
    """Fallback: a before/after chart from sentences that contain >= 2 numbers."""
    series, used = [], []
    for item in items:
        if item.get("cells") or item.get("kind") != "number":
            continue
        nums = item.get("numbers") or []
        if len(nums) < 2:
            continue
        # a series name has to fit a legend column; the sentence prefix is far too
        # long, so use the short subject phrase the number belongs to
        label = _subject_of_number(item) or _clean_label(item.get("claim"), 16)
        unit = item.get("unit") or "AU"
        name = label
        series.append({"name": name[:18], "values": [round(nums[0], 6), round(nums[-1], 6)],
                       "source": item["id"]})
        used.append(item["id"])
        if len(series) >= MAX_SERIES:
            break
    if not series:
        return [], [], ["no chart could be built: no numeric series and no paired numbers in the text"]
    chart = {"id": scene.stable_id("C", "inline-numbers"), "type": "bar",
             "title": "Reported change (first vs last value in sentence)", "unit": series[0] and
             (next((i.get("unit") for i in items if i["id"] == series[0]["source"]), None) or "AU"),
             "categories": ["start", "end"], "series": series}
    return [chart], used, ["charts built from inline numbers in the text (no table available)"]


# --------------------------------------------------------------------------- #
# panels
# --------------------------------------------------------------------------- #
def _role_of(item):
    section = (item.get("section") or "").lower()
    for role in ROLE_ORDER:
        for kw in ROLE_KEYWORDS[role]:
            if kw in section:
                return role
    kind = item.get("kind")
    if kind == "condition" and "method" in ev._norm(item.get("claim") or "").lower():
        return "method"
    return None


def _entity_names(item):
    """Entity-ish names contributed by one evidence item, in order."""
    names = []
    for rel in (item.get("relations") or []):
        names.append(rel.get("subject"))
        names.append(rel.get("object"))
    if not names:
        if item.get("kind") == "number" and item.get("value") is not None:
            # a measured quantity is an entity too: label it with the phrase the
            # number belongs to, so the results panel actually shows the data
            names.append(_subject_of_number(item))
        elif item.get("kind") in ("entity", "condition"):
            claim = _clean_label(item.get("claim"), 16)
            if 2 <= len(claim) <= 30:
                names.append(claim)
    out = []
    for n in names:
        n = ev._clean_term(n) if n else None
        if n and n.lower() not in [o.lower() for o in out]:
            out.append(n)
    return out


def _subject_of_number(item):
    """The phrase immediately before the first number of a measured claim.

    Kept deliberately short: PDF-extracted sentences are long, and a long label
    is what makes the layout engine drop its last token (gate B/F). Four words
    is enough to identify the quantity and reliably fits a node box.
    """
    claim = ev._norm(item.get("claim") or "")
    m = re.search(r"(?<![A-Za-z0-9._\-])-?\d", claim)
    prefix = claim[:m.start()] if m else claim
    prefix = re.sub(r"[\(\[]\s*$", "", prefix).strip(" ,;:-")
    words = [w for w in prefix.split(" ") if w]
    if len(words) > 4:
        words = words[-4:]
    label = " ".join(words)
    label = re.sub(r"^(?:and|the|a|an|of|in|was|were|is|are|by|that|which)\s+", "", label, flags=re.I)
    return _clean_label(label, 16)


def build_panels(materials, ledger):
    items = list(ledger.get("items") or [])
    if not items:
        items = ev.fallback_evidence(materials)
    charts, chart_ev, chart_notes = charts_from_tables(materials, items)
    if not charts:
        alt, alt_ev, alt_notes = charts_from_numbers(items)
        charts, chart_ev, chart_notes = alt, alt_ev, chart_notes + alt_notes

    # assign items to roles, first come first served, then fill empty roles
    by_role = {r: [] for r in ROLE_ORDER}
    unassigned = []
    for item in items:
        role = _role_of(item)
        (by_role[role] if role else unassigned).append(item)
    for role in ROLE_ORDER:
        while not by_role[role] and unassigned:
            by_role[role].append(unassigned.pop(0))
    # leftovers go to results (they are the densest panel) without breaking order
    by_role["results"].extend(unassigned)

    # charts belong to the panel that owns the data: results if it has numbers
    chart_panel = {}
    for chart in charts:
        chart_panel[chart["id"]] = "results" if any(
            it.get("kind") == "number" for it in by_role["results"]) else "method"

    role_titles = {
        "problem": "Problem: what the material sets out to resolve",
        "method": "Method: how the material was produced",
        "results": "Results: the measured outcome",
        "conclusion": "Conclusion: what the results support",
    }
    if _cjk_ratio((materials.get("task_text") or "") + " " +
                  " ".join((m.get("text") or "")[:2000] for m in (materials.get("materials") or []))) > 0.15:
        role_titles = {
            "problem": "\u95ee\u9898\uff1a\u6750\u6599\u8981\u89e3\u51b3\u4ec0\u4e48",
            "method": "\u65b9\u6cd5\uff1a\u6750\u6599\u662f\u600e\u4e48\u5f97\u6765\u7684",
            "results": "\u7ed3\u679c\uff1a\u5b9e\u6d4b\u5230\u4e86\u4ec0\u4e48",
            "conclusion": "\u7ed3\u8bba\uff1a\u7ed3\u679c\u652f\u6301\u4ec0\u4e48",
        }

    panels, used_evidence = [], []
    global_nodes = {}
    valued_nodes = []
    for ri, role in enumerate(ROLE_ORDER):
        role_items = by_role[role]
        cap = MAX_NODES_PER_PANEL.get(role, 6)
        nodes, node_ids = [], []
        for item in role_items:
            for name in _entity_names(item):
                if len(nodes) >= cap:
                    break
                nid = scene.stable_id("N", name.lower())
                if nid in global_nodes:
                    continue
                value, unit = (item.get("value"), item.get("unit")) if item.get("kind") == "number" else (None, None)
                node = {"id": nid, "shape": "hexagon" if item.get("kind") == "relation" else "rrect",
                        "label": _clean_label(name, 18), "source": item["id"]}
                if value is not None:
                    node["sublabel"] = ("%g" % value) + (" " + unit if unit else "")
                    node["value"] = float(value)
                    node["unit"] = unit
                    valued_nodes.append((nid, float(value), unit))
                nodes.append(node)
                node_ids.append(nid)
                global_nodes[nid] = role
                used_evidence.append(item["id"])
            if len(nodes) >= cap:
                break

        nodes_by_name = {n["label"].lower(): n["id"] for n in nodes}
        edges = []
        for item in role_items:
            if len(edges) >= MAX_EDGES_PER_PANEL:
                break
            for rel in (item.get("relations") or []):
                subj = ev._clean_term(rel.get("subject")) or ""
                obj = ev._clean_term(rel.get("object")) or ""
                if not subj or not obj:
                    continue
                for name in (subj, obj):
                    if name.lower() not in nodes_by_name:
                        if len(nodes) >= cap:
                            break
                        nid = scene.stable_id("N", name.lower())
                        if nid in global_nodes:
                            break
                        nodes.append({"id": nid, "shape": "rrect",
                                      "label": _clean_label(name, 18), "source": item["id"]})
                        node_ids.append(nid)
                        nodes_by_name[name.lower()] = nid
                        global_nodes[nid] = role
                        used_evidence.append(item["id"])
                a, b = nodes_by_name.get(subj.lower()), nodes_by_name.get(obj.lower())
                if not a or not b or a == b:
                    continue
                if any(e["from"] == a and e["to"] == b for e in edges):
                    continue
                edges.append({"id": scene.stable_id("R", "%s|%s|%s" % (a, b, rel.get("kind"))),
                              "from": a, "to": b, "kind": rel.get("kind") or "activation",
                              "label": _clean_label(rel.get("verb") or "", 18),
                              "source": item["id"]})
                used_evidence.append(item["id"])

        panel = {"id": "P%d" % (ri + 1), "role": role,
                 "title": role_titles[role],
                 "nodes": nodes, "edges": edges, "charts": []}
        for chart in charts:
            if chart_panel.get(chart["id"]) == role:
                panel["charts"].append(chart)
                used_evidence.extend(chart_ev)
        if panel["charts"]:
            labels, seen = [], set()
            for chart in panel["charts"]:
                for s in chart["series"]:
                    key = s["name"].lower()
                    if key not in seen:
                        seen.add(key)
                        labels.append(s["name"])
            panel["legend"] = {"title": "series", "items": [
                {"label": lab, "color": col}
                for lab, col in zip(labels, scene.categorical_colors(len(labels)))]}
        # the panel must not be empty: keep a labelled claim node
        if not panel["nodes"] and role_items:
            it = role_items[0]
            nid = scene.stable_id("N", _clean_label(it.get("claim"), 16).lower())
            if nid not in global_nodes:
                panel["nodes"].append({"id": nid, "shape": "rrect",
                                       "label": _clean_label(it.get("claim"), 18),
                                       "source": it["id"]})
                global_nodes[nid] = role
                used_evidence.append(it["id"])
        panels.append(panel)

    # colourbar over the encoded values (same channel the node fills use).
    # Its range must cover exactly what it encodes: mixing units (a 21 percent
    # reading next to 0.19 per second) produces a bar whose printed ends match
    # nothing on the figure, which gate D rejects.
    if valued_nodes:
        units = {}
        for _, _, u in valued_nodes:
            units[u] = units.get(u, 0) + 1
        unit = sorted(units.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]
        target = max(panels, key=lambda p: sum(1 for n in p["nodes"]
                                               if isinstance(n.get("value"), (int, float))
                                               and n.get("unit") == unit))
        cb_vals = [n["value"] for n in target["nodes"]
                   if isinstance(n.get("value"), (int, float)) and n.get("unit") == unit]
        if len(cb_vals) < 2:
            # a colour channel needs at least two encoded values to be meaningful;
            # without that the bar would describe data the panel does not show.
            pass
        else:
            target.setdefault("colorbars", []).append({
                "id": scene.stable_id("CB", "%s|%s" % (target["id"], unit)),
                "title": "value encoded by node fill", "unit": unit or "AU",
                "vmin": round(min(cb_vals), 6), "vmax": round(max(cb_vals), 6),
                "cmap": "viridis"})
    # a supplied structure file is a workorder: make sure every declared relation
    # is actually drawn, otherwise the fallback figure can silently omit the
    # paper's headline arrow
    declared = ev.workorder_relations(materials)
    if declared:
        target = panels[2] if len(panels) > 2 else panels[-1]      # results panel
        by_label = {}
        for p in panels:
            for n in p["nodes"]:
                by_label.setdefault(ev._norm_label(n["label"]), n["id"])
        wired = {(e["from"], e["to"]) for p in panels for e in p["edges"]}
        drawn = 0
        for rel in declared:
            a = _resolve_label(rel["subject"], by_label)
            b = _resolve_label(rel["object"], by_label)
            if not a or not b or a == b:
                continue
            if (a, b) in wired:
                drawn += 1
                continue
            if (b, a) in wired:
                # the figure would contradict the workorder: flip it rather than
                # ship a reversed arrow
                for p in panels:
                    for e in p["edges"]:
                        if e["from"] == b and e["to"] == a:
                            e["from"], e["to"] = a, b
                drawn += 1
                continue
            kind = rel["kind"] if rel["kind"] in scene.EDGE_KINDS else "activation"
            # draw the declared relation in whichever panel already holds its
            # subject, instead of forcing it into the results panel
            host = next((p for p in panels if a in {n["id"] for n in p["nodes"]}), None)
            if host is None:
                continue
            src = next((n.get("source") for n in host["nodes"]
                        if n["id"] == a and n.get("source")), None)
            if not src:
                src = next((n.get("source") for n in host["nodes"] if n.get("source")), None)
            if not src:
                continue          # repair() would drop a sourceless edge anyway
            host["edges"].append({"id": scene.stable_id("R", "%s|%s|%s" % (a, b, kind)),
                                  "from": a, "to": b, "kind": kind,
                                  "label": _clean_label(rel["subject"], 18),
                                  "source": src})
            wired.add((a, b))
            drawn += 1
        chart_notes.append("workorder: %d declared relation(s) represented in the figure" % drawn)

    return panels, used_evidence, chart_notes


def _resolve_label(name, by_label):
    key = ev._norm_label(name)
    if key in by_label:
        return by_label[key]
    for lab, nid in by_label.items():
        if lab and (lab in key or key in lab):
            return nid
    return None


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #
def _title(materials, ledger, case_id):
    for item in (ledger.get("items") or []):
        claim = ev._norm(item.get("claim") or "")
        claim = re.sub(r"^(?:title|heading)\s*[:\-]\s*", "", claim, flags=re.I)
        if item.get("kind") == "entity" or claim.endswith(":"):
            continue
        if 20 <= len(claim) <= 120:
            return _clean_label(claim, 96)
    task = ev._norm(materials.get("task_text") or "")
    if task:
        return _clean_label(task, 96)
    return "Graphical abstract for %s" % case_id


def derive_spec(materials, ledger):
    case_id = materials.get("case_id") or "unknown-case"
    items = list(ledger.get("items") or [])
    if not items:
        items = ev.fallback_evidence(materials)
        ledger = {"items": items, "counts": {"items": len(items)}, "warnings": []}

    language = "zh" if _cjk_ratio(" ".join(
        (m.get("text") or "")[:4000] for m in (materials.get("materials") or []))) > 0.15 else "en"

    title = _title(materials, {"items": items}, case_id)
    panels, used, notes = build_panels(materials, {"items": items})

    # only ship evidence that is cited by something, plus any number evidence the
    # charts rely on; this keeps the ledger auditable instead of a wall of text
    cited = []
    seen = set()
    for eid in used:
        if eid and eid not in seen:
            seen.add(eid)
            cited.append(eid)
    if not cited:
        cited = [items[0]["id"]] if items else []
    evidence_items = []
    for eid in cited:
        for item in items:
            if item.get("id") == eid:
                evidence_items.append(item)
                break
    if not evidence_items:
        evidence_items = ev.fallback_evidence(materials)

    warnings = list(materials.get("warnings") or [])
    uncertainty = []
    for w in warnings:
        uncertainty.append({"kind": w.get("kind"), "subject": w.get("subject"),
                            "reason": w.get("reason"), "needs_human": bool(w.get("needs_human"))})
    limitations = []
    for note in notes:
        limitations.append(note)
    if not materials.get("parsable"):
        limitations.append("No material file yielded readable text; the figure can only restate "
                           "the task/input metadata and makes no scientific claim.")
    if len(panels[2]["nodes"]) == 0:
        limitations.append("No numeric result could be attributed to a source span; the results "
                           "panel carries only qualitative claims.")

    spec = {
        "version": scene.VERSION,
        "meta": {
            "case_id": case_id,
            "title": title,
            "subtitle": _clean_label(ev._norm(materials.get("task_text") or ""), 140),
            "language": language,
            "canvas": dict(AUTOSPEC_CANVAS),
            "layout": {"columns": 2, "row_order": list(ROLE_ORDER)},
            "generated_by": "autospec (deterministic fallback, no LLM)",
        },
        "palette": {"id": "okabe-ito", "cvd_safe": True},
        "texts": {
            "caption": "Single-page graphical abstract derived automatically from the supplied "
                       "material. Every element carries an evidence id that resolves to a verbatim "
                       "quote in a source file.",
            "conclusion": _conclusion_text(panels),
        },
        "evidence": evidence_items,
        "panels": panels,
        "uncertainty": uncertainty,
        "limitations": limitations,
        "change_records": [{"kind": "autospec", "source": "materials.json + evidence-ledger.json",
                            "result": "deterministic spec", "human_edit": False}],
    }
    spec = repair(spec)
    return spec


def _conclusion_text(panels):
    for panel in panels:
        if panel.get("role") == "conclusion":
            labels = [n.get("label") for n in panel.get("nodes") or []]
            if labels:
                return "; ".join(labels[:3]) + "."
    return ""


def repair(spec):
    """Deterministic repair: drop anything the model cannot substantiate."""
    evidence_ids = {e.get("id") for e in (spec.get("evidence") or [])}
    if not evidence_ids:
        spec["evidence"] = []
        return spec
    for panel in spec.get("panels") or []:
        nodes = [n for n in (panel.get("nodes") or [])
                 if n.get("id") and n.get("label") and n.get("source") in evidence_ids]
        ids = {n["id"] for n in nodes}
        panel["nodes"] = nodes
        edges = []
        for e in panel.get("edges") or []:
            if e.get("from") in ids and e.get("to") in ids and e.get("from") != e.get("to") \
                    and e.get("kind") in scene.EDGE_KINDS and e.get("source") in evidence_ids:
                edges.append(e)
        panel["edges"] = edges
        charts = []
        for c in panel.get("charts") or []:
            if c.get("type") not in scene.CHART_TYPES:
                continue
            series = [s for s in (c.get("series") or [])
                      if s.get("name") and isinstance(s.get("values"), list) and s["values"]
                      and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in s["values"])]
            if not series:
                continue
            # keep only source references that actually exist
            for s in series:
                keep = [r for r in (s.get("sources") or []) if r in evidence_ids]
                if keep:
                    s["sources"] = keep
                else:
                    s.pop("sources", None)
                if s.get("source") not in evidence_ids:
                    s.pop("source", None)
            series = [s for s in series if s.get("source") or s.get("sources")]
            if not series:
                continue
            c["series"] = series
            if not c.get("unit"):
                c["unit"] = "AU"
            charts.append(c)
        panel["charts"] = charts
        if panel.get("legend") is not None:
            names = []
            for c in charts:
                for s in c["series"]:
                    if s["name"] not in names:
                        names.append(s["name"])
            items = [i for i in (panel["legend"].get("items") or [])
                     if isinstance(i, dict) and i.get("label")]
            if names:
                palette = scene.PALETTES["okabe-ito"]
                panel["legend"] = {"title": panel["legend"].get("title") or "series",
                                   "items": [{"label": n, "color": palette[k % len(palette)]}
                                             for k, n in enumerate(names)]}
            elif items:
                panel["legend"] = {"title": panel["legend"].get("title") or "series", "items": items}
            else:
                panel.pop("legend", None)
        for cb in panel.get("colorbars") or []:
            if not (isinstance(cb.get("vmin"), (int, float)) and isinstance(cb.get("vmax"), (int, float))):
                continue
            if cb["vmax"] <= cb["vmin"]:
                cb["vmax"] = cb["vmin"] + 1.0
            if cb.get("cmap") not in scene.CMAPS:
                cb["cmap"] = "viridis"
            if not cb.get("unit"):
                cb["unit"] = "AU"
        cbs = []
        for cb in panel.get("colorbars") or []:
            if isinstance(cb.get("vmin"), (int, float)) and isinstance(cb.get("vmax"), (int, float)) \
                    and cb["vmax"] > cb["vmin"] and cb.get("cmap") in scene.CMAPS:
                cbs.append(cb)
        if cbs:
            panel["colorbars"] = cbs
        else:
            panel.pop("colorbars", None)

    # role coverage is part of the product, not just a warning
    present = {p.get("role") for p in spec.get("panels") or []}
    for role in ROLE_ORDER:
        if role not in present:
            spec.setdefault("panels", []).append({
                "id": "P%d" % (len(spec["panels"]) + 1), "role": role,
                "title": "(%s panel could not be derived from the material)" % role,
                "nodes": [], "edges": [], "charts": []})
    return spec


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(description="derive a spec without an LLM")
    ap.add_argument("--materials", required=True)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    with open(a.materials, "r", encoding="utf-8-sig") as fh:
        materials = json.load(fh)
    with open(a.ledger, "r", encoding="utf-8-sig") as fh:
        ledger = json.load(fh)
    spec = derive_spec(materials, ledger)
    issues = scene.validate_spec(spec)
    errors = [i for i in issues if i["severity"] == "error"]
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    sys.stderr.write("[autospec] panels=%d evidence=%d errors=%d\n"
                     % (len(spec["panels"]), len(spec["evidence"]), len(errors)))
    for e in errors:
        sys.stderr.write("  ERR %s %s %s\n" % (e["code"], e["path"], e["message"]))
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

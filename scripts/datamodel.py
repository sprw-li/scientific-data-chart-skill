#!/usr/bin/env python3
"""datamodel.py — coordinate-addressed typed tables built from REAL material files.

Why this module exists
----------------------
The Skill's claim is "every drawn mark can be traced back to
``file#table:row:column``".  That claim is only checkable if the tables have
REAL coordinates: a header row that can be quoted, a 1-based row number that
matches the record/line on disk, and a column name taken verbatim from the
source header.  ``ingest.py`` extracts text + spans; this module turns that
extraction into an explicit, addressable table model.

Frozen API (CONTRACT §4.3)
--------------------------
    build_tables(materials, input_dir=None) -> dict
        {"schema_version": .., "tables": [{name, file, format, header_row,
          n_rows, columns, rows}], "files": [...], "counts": {...}}

Recognised table forms — detected from CONTENT, never from the file name:

* delimited text (CSV/TSV/semicolon/pipe)  -- quote-aware, record numbering
  identical to ``ingest.py``'s ``row N`` locators so evidence cross-references,
* Markdown pipe tables (header + ``---`` separator + body) -- row numbers are
  physical 1-based line numbers,
* JSON list-of-objects / list-of-lists,
* whitespace-aligned text (PDF text layer / plain-text dumps) -- flagged
  ``inferred: true`` because alignment is a heuristic,
* XLSX sheets (re-uses what ``ingest.py`` already decoded).

Numbers are normalised for LOOKUP only; the original cell text is always kept so
a quote stays verbatim.  Handled: UTF-8 BOM, CRLF, thousands separators,
scientific notation, percent signs, empty cells, mixed numeric/text columns,
ragged rows and CJK headers.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys

sys.dont_write_bytecode = True

SCHEMA_VERSION = "1.0"

MAX_TABLE_ROWS = 4000
MAX_TABLE_COLS = 128
MAX_MARKDOWN_LINES = 6000
MAX_JSON_TABLE_ROWS = 2000

CJK_RANGE = "\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f"
_PAREN_UNIT = re.compile(r"[\(\[]\s*([^\)\]]{1,24})\s*[\)\]]")
_NON_LABEL = re.compile(r"[^0-9a-z%s\u0370-\u03ff]+" % CJK_RANGE)
_INDEX_HEADER = re.compile(r"^\s*(?:col(?:umn)?\s*)?(\d+)\s*$", re.I)
_COL_LETTERS = re.compile(r"^\s*([A-Za-z]{1,3})\s*$")
_MD_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_ALIGNED_SPLIT = re.compile(r"(?:\t+|(?:\s{2,}))")


# --------------------------------------------------------------------------- #
# small numeric / label helpers (shared with datamap.py)
# --------------------------------------------------------------------------- #
def parse_number(value):
    """Best-effort numeric value of a cell; ``None`` when it is not a number.

    Accepts ``1,234.5``, ``1.5e-3``, ``12.5%``, unicode minus and thin spaces.
    A cell that mixes a number with text (``3.2 (n=3)``) stays non-numeric on
    purpose: inventing a value there would let a fabricated number pass.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("\u2212", "-").replace("\u00a0", " ").replace("\u2009", " ").strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    s = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", s)     # thousands separators only
    s = s.replace(" ", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def norm_label(text):
    """Comparison form of a label: case-folded, unit-stripped, punctuation-free.

    ``MitoSOX_fold`` -> ``mitosox fold``; ``MitoSOX (fold)`` -> ``mitosox``.
    Used ONLY for matching; displayed text always keeps the original spelling.
    """
    s = str(text or "")
    m = _PAREN_UNIT.search(s)
    unit = m.group(1) if m else None
    if m:
        s = s[:m.start()] + " " + s[m.end():]
    s = (s.replace("_", " ").replace("-", " ")
         .replace("\u03b1", "alpha").replace("\u03b2", "beta").lower())
    out = _NON_LABEL.sub(" ", s).strip()
    if not out and unit:
        out = _NON_LABEL.sub(" ", unit.lower()).strip()
    return re.sub(r"\s+", " ", out).strip()


def header_unit(label):
    """Unit declared in a header, e.g. ``ATP (nmol/mg)`` -> ``nmol/mg``."""
    m = _PAREN_UNIT.search(str(label or ""))
    if m:
        return m.group(1).strip()
    m2 = re.search(r"(?:^|_)(fold|pct|percent|pmol|nmol|mmol|umol|mph|ph|min|sec|kda|bp|nt)$",
                   str(label or ""), re.I)
    return m2.group(1) if m2 else None


def norm_path(p):
    return str(p or "").replace("\\", "/")


def cell_key(file_ref, table_name, row, column):
    """The frozen cell key: ``file#table:row:column`` (CONTRACT §4.1)."""
    return "%s#%s:%s:%s" % (norm_path(file_ref), table_name, row, column)


# --------------------------------------------------------------------------- #
# delimited text
# --------------------------------------------------------------------------- #
def _delimiter_for(text):
    lines = [l for l in text.split("\n")[:30] if l.strip()]
    if not lines:
        return None
    best, best_score = None, 0
    for d in ("\t", ",", ";", "|"):
        counts = []
        for line in lines[:10]:
            counts.append(max(0, line.count(d) - 1) if d == "|" else line.count(d))
        if not counts or counts[0] < 1:
            continue
        first = counts[0]
        agree = sum(1 for c in counts if c == first) / float(len(counts))
        if agree >= 0.6 and first > best_score:
            best, best_score = d, first
    if best == "|" and any(_MD_SEP.match(l) for l in lines[:4]):
        return None                        # a Markdown pipe table: own parser
    return best


def _split_raw_spans(text):
    """(start, end) of every physical record in ``text`` (quote aware)."""
    spans, i, n, start, in_q = [], 0, len(text), 0, False
    while i < n:
        c = text[i]
        if in_q:
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    i += 2
                    continue
                in_q = False
            i += 1
            continue
        if c == '"':
            in_q = True
        elif c == "\n":
            spans.append((start, i))
            start = i + 1
        i += 1
    if start < n:
        spans.append((start, n))
    return spans


def _column_names(header, width):
    """Header labels, de-duplicated; empty names become addressable ``colN``."""
    cols, seen = [], {}
    for i in range(width):
        raw = str(header[i]).strip() if i < len(header) and header[i] is not None else ""
        name = raw if raw else "col%d" % (i + 1)
        if name in seen:
            seen[name] += 1
            name = "%s__%d" % (name, seen[name])
        else:
            seen[name] = 1
        cols.append(name)
    return cols


def _delim_tables(text, table_name):
    d = _delimiter_for(text)
    if d is None:
        return [], None
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), delimiter=d))
    except Exception as exc:  # noqa: BLE001
        return [], "csv-error: %s" % exc
    spans = _split_raw_spans(text)
    if d == "|":
        records = [[c.strip() for c in r] for r in records]
    while records and not any(str(c).strip() for c in records[-1]):
        records.pop()
    while records and not any(str(c).strip() for c in records[0]):
        records.pop(0)
    if len(records) < 2:
        return [], "only %d usable record(s); not a table" % len(records)
    widths = {}
    for r in records:
        widths[len(r)] = widths.get(len(r), 0) + 1
    width = max(widths, key=lambda w: (widths[w], w))
    cols = _column_names([str(c).strip() for c in records[0]], width)
    rows = []
    for i, rec in enumerate(records[1:], start=2):
        if not any(str(c).strip() for c in rec):
            continue
        cells = [str(c) for c in rec]
        while len(cells) < len(cols):
            cells.append("")
        span = spans[i - 1] if i - 1 < len(spans) else None
        rows.append({"row": i, "cells": cells[:len(cols)],
                     "start": span[0] if span else None,
                     "end": span[1] if span else None})
        if len(rows) >= MAX_TABLE_ROWS:
            break
    if not rows:
        return [], "header but no data record"
    note = None
    if len(widths) > 1:
        note = ("%d record(s) differ in field count from the modal width %d; short rows are "
                "padded with empty strings and no cell is invented"
                % (sum(v for k, v in widths.items() if k != width), width))
    return [{
        "name": table_name, "source": "delimited",
        "format": "tsv" if d == "\t" else "csv", "delimiter": d,
        "header_row": 1, "columns": cols, "rows": rows, "n_rows": len(rows),
        "inferred": False, "note": note,
    }], None


# --------------------------------------------------------------------------- #
# Markdown pipe tables
# --------------------------------------------------------------------------- #
def _markdown_tables(text, table_name):
    lines = text.split("\n")
    out, i, idx = [], 0, 0
    while i < len(lines) - 1 and i < MAX_MARKDOWN_LINES:
        if "|" not in lines[i] or not _MD_SEP.match(lines[i + 1]):
            i += 1
            continue
        block = [lines[i]]
        j = i + 2
        while j < len(lines) and "|" in lines[j] and lines[j].strip():
            block.append(lines[j])
            j += 1
        if len(block) < 2:
            i = j
            continue
        idx += 1

        def cells_of(line):
            s = line.strip()
            if s.startswith("|"):
                s = s[1:]
            if s.endswith("|"):
                s = s[:-1]
            return [c.strip() for c in s.split("|")]

        header = cells_of(block[0])
        rows = []
        for k, line in enumerate(block[1:], start=i + 3):
            cs = cells_of(line)
            while len(cs) < len(header):
                cs.append("")
            rows.append({"row": k, "cells": cs[:len(header)]})
            if len(rows) >= MAX_TABLE_ROWS:
                break
        out.append({
            "name": "%s.md%d" % (table_name, idx) if idx > 1 else table_name,
            "source": "markdown-pipe-table", "format": "md", "delimiter": "|",
            "header_row": i + 1, "columns": _column_names(header, len(header)),
            "rows": rows, "n_rows": len(rows), "inferred": False,
            "note": "row numbers are physical 1-based line numbers in the file",
        })
        i = j
        if len(out) >= 12:
            break
    return out


# --------------------------------------------------------------------------- #
# JSON tables
# --------------------------------------------------------------------------- #
def _json_cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _json_tables(value, table_name, ptr="$"):
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.extend(_json_tables(v, table_name, "%s.%s" % (ptr, k)))
            if len(out) >= 8:
                return out[:8]
        return out
    if not isinstance(value, list) or len(value) < 2:
        return out
    rows_raw, columns = [], None
    if all(isinstance(x, dict) for x in value):
        seen = []
        for obj in value:
            for k in obj:
                if k not in seen:
                    seen.append(k)
        if not seen:
            return out
        columns = [str(k) for k in seen]
        for i, obj in enumerate(value, start=1):
            rows_raw.append({"row": i, "cells": [_json_cell(obj.get(k)) for k in seen]})
    elif all(isinstance(x, list) for x in value) and len({len(x) for x in value}) == 1:
        width = len(value[0])
        if width < 2:
            return out
        first_is_text = not all(parse_number(_json_cell(c)) is not None for c in value[0])
        header = ([str(c) for c in value[0]] if first_is_text
                  else ["col%d" % (i + 1) for i in range(width)])
        body = value[1:] if first_is_text else value
        off = 1 if first_is_text else 0
        columns = _column_names(header, width)
        for i, row in enumerate(body, start=1 + off):
            rows_raw.append({"row": i, "cells": [_json_cell(c) for c in row]})
    else:
        return out
    if not rows_raw:
        return out
    cols = _column_names(columns, len(columns))
    if len(cols) < 2:
        return out
    if not any(sum(1 for r in rows_raw if parse_number(r["cells"][ci]) is not None) >= 1
               for ci in range(len(cols))):
        return out
    out.append({
        "name": table_name, "source": "json-array", "format": "json", "pointer": ptr,
        "header_row": 0, "columns": cols, "rows": rows_raw[:MAX_JSON_TABLE_ROWS],
        "n_rows": min(len(rows_raw), MAX_JSON_TABLE_ROWS), "inferred": False,
        "note": "rows are 1-based array indexes; header_row 0 means the column names are keys",
    })
    return out


# --------------------------------------------------------------------------- #
# whitespace-aligned text (PDF text layer / plain-text dumps)
# --------------------------------------------------------------------------- #
def _aligned_block(block, table_name, idx):
    if len(block) < 3:
        return None
    width = len(block[0][1])
    if width < 2:
        return None
    header = block[0][1]
    if all(parse_number(c) is not None for c in header):
        return None                        # first row is data, not a header
    body = block[1:]
    if not any(all(parse_number(c) is not None for c in r[1]) for r in body):
        return None
    rows = [{"row": ln, "cells": list(f)} for ln, f in body]
    return {
        "name": "%s.pdfT%d" % (table_name, idx) if idx > 1 else table_name,
        "source": "aligned-text", "format": "pdf-text",
        "header_row": block[0][0], "columns": _column_names(header, width),
        "rows": rows[:MAX_TABLE_ROWS], "n_rows": min(len(rows), MAX_TABLE_ROWS),
        "inferred": True,
        "note": "aligned by whitespace runs in an extracted text layer; column boundaries "
                "are a heuristic, cells stay verbatim line fragments",
    }


def _aligned_tables(text, table_name, max_tables=4):
    out, cur = [], []
    lines = text.split("\n")
    for i, line in enumerate(lines, start=1):
        stripped = line.rstrip().lstrip("\f") if line.lstrip().startswith("\f") else line.rstrip()
        if not stripped.strip():
            if len(cur) >= 3:
                t = _aligned_block(list(cur), table_name, len(out) + 1)
                if t:
                    out.append(t)
            cur = []
            continue
        fields = [f for f in _ALIGNED_SPLIT.split(stripped.strip()) if f != ""]
        if len(fields) < 2:
            if len(cur) >= 3:
                t = _aligned_block(list(cur), table_name, len(out) + 1)
                if t:
                    out.append(t)
            cur = []
            continue
        if cur and len(fields) != len(cur[-1][1]):
            if len(cur) >= 3:
                t = _aligned_block(list(cur), table_name, len(out) + 1)
                if t:
                    out.append(t)
            cur = []
        cur.append((i, fields))
    if len(cur) >= 3:
        t = _aligned_block(list(cur), table_name, len(out) + 1)
        if t:
            out.append(t)
    return out[:max_tables]


# --------------------------------------------------------------------------- #
# ingest-produced tables (CSV/TSV/XLSX) -> the same coordinate model
# --------------------------------------------------------------------------- #
def _ingest_tables(mat, table_name):
    out = []
    for ti, t in enumerate(mat.get("tables") or []):
        header = [str(h) for h in (t.get("header") or [])]
        rows_in = t.get("rows") or []
        if not rows_in:
            continue
        width = len(header)
        for r in rows_in:
            width = max(width, len(r.get("cells") or []))
        if width < 1:
            continue
        cols = _column_names(header, width)
        rows = []
        for r in rows_in:
            cells = [str(c) for c in (r.get("cells") or [])]
            while len(cells) < len(cols):
                cells.append("")
            rows.append({"row": int(r.get("n") or (len(rows) + 2)), "cells": cells[:len(cols)],
                         "start": r.get("start"), "end": r.get("end")})
        hr = t.get("header_row")
        hr_n = int(hr.get("n")) if isinstance(hr, dict) and hr.get("n") else 1
        src = t.get("source") or "csv"
        name = table_name if ti == 0 else "%s.t%d" % (table_name, ti + 1)
        out.append({
            "name": name, "source": src,
            "format": "tsv" if t.get("delimiter") == "\t" else ("xlsx" if src == "xlsx" else "csv"),
            "delimiter": t.get("delimiter"), "header_row": hr_n, "columns": cols,
            "rows": rows[:MAX_TABLE_ROWS], "n_rows": min(len(rows), MAX_TABLE_ROWS),
            "inferred": False, "note": None,
        })
    return out


# --------------------------------------------------------------------------- #
# per-file dispatch
# --------------------------------------------------------------------------- #
def _looks_json(text):
    return text.lstrip()[:1] in ("{", "[")


def _file_tables(mat, stem):
    """-> (tables, format_hint) for one material entry."""
    tables = []
    text = mat.get("text") or ""
    ext = os.path.splitext(norm_path(mat.get("path")))[1].lower()
    fmt = None
    ingest_tables = _ingest_tables(mat, stem)
    if ingest_tables:
        tables.extend(ingest_tables)
        fmt = ingest_tables[0]["format"]
    if tables or not text.strip():
        return tables, fmt
    if ext in (".md", ".markdown") or "|" in text:
        md = _markdown_tables(text, stem)
        if md:
            return md, "md"
    if _looks_json(text):
        try:
            value = json.loads(text)
        except Exception:  # noqa: BLE001
            value = None
        if value is not None:
            jt = _json_tables(value, stem)
            if jt:
                return jt, "json"
    if ext in (".txt", ".log") or mat.get("kind") in ("text", "pdf-text", "markdown"):
        at = _aligned_tables(text, stem)
        if at:
            return at, "pdf-text"
    dl, _err = _delim_tables(text, stem)
    if dl:
        return dl, dl[0]["format"]
    return [], fmt


def _entry_for(mat):
    rel = norm_path(mat.get("path"))
    stem = os.path.splitext(os.path.basename(rel))[0] or rel
    tables, fmt = _file_tables(mat, stem)
    for t in tables:
        t["file"] = rel
    return {
        "file": rel,
        "sha256": mat.get("sha256"),
        "format": fmt or (mat.get("kind") or "unknown"),
        "kind": mat.get("kind"),
        "role": mat.get("role"),
        "parsed": bool(mat.get("parsed")),
        "n_tables": len(tables),
    }, tables


def _tables_from_dir(input_dir):
    """Fallback: read files straight off disk (no ingest result available)."""
    mats = []
    for root, _dirs, files in os.walk(input_dir):
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = norm_path(os.path.relpath(full, input_dir))
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            text = None
            for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
                try:
                    text = data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            mats.append({
                "path": rel, "role": "case" if rel == "case.json" else "material",
                "sha256": _sha256_bytes(data), "kind": None, "parsed": text is not None,
                "text": (text or "").replace("\r\n", "\n").replace("\r", "\n"),
                "tables": [],
            })
    return mats


def _sha256_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# public API (CONTRACT §4.3)
# --------------------------------------------------------------------------- #
def build_tables(materials, input_dir=None):
    """-> ``data-tables.json``: every table of every material, coordinate-addressed.

    ``materials`` is the ``ingest`` result (or any mapping with a ``materials``
    list).  When it is empty/absent and ``input_dir`` is given, files are read
    straight off disk so the function is usable standalone.
    """
    materials = materials or {}
    mats = list(materials.get("materials") or [])
    if not mats and input_dir and os.path.isdir(input_dir):
        mats = _tables_from_dir(input_dir)
    files, tables, warnings = [], [], []
    for mat in mats:
        if mat.get("role") == "case":
            continue
        entry, ts = _entry_for(mat)
        for t in ts:
            if t.get("note"):
                warnings.append({"kind": "table_note",
                                 "subject": "%s#%s" % (t["file"], t["name"]),
                                 "reason": t["note"]})
        files.append(entry)
        tables.extend(ts)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "case_id": materials.get("case_id") or "",
        "cell_syntax": "<file>#<table>:<row>:<column> — row is the 1-based record number "
                       "(header_row names the header record), column is the verbatim header label",
        "tables": tables,
        "files": files,
        "counts": {
            "files": len(files),
            "files_with_tables": len([f for f in files if f["n_tables"]]),
            "tables": len(tables),
            "rows": sum(t["n_rows"] for t in tables),
            "cells": sum(t["n_rows"] * len(t["columns"]) for t in tables),
            "numeric_cells": sum(1 for t in tables for r in t["rows"] for c in r["cells"]
                                 if parse_number(c) is not None),
            "inferred_tables": len([t for t in tables if t.get("inferred")]),
        },
        "warnings": warnings,
    }
    return doc


# --------------------------------------------------------------------------- #
# queries over the table model
# --------------------------------------------------------------------------- #
def iter_cells(dt):
    """Every cell, coordinate-addressed and ready for matching."""
    for t in (dt.get("tables") or []):
        cols = t.get("columns") or []
        file_ref = t.get("file")
        for r in t.get("rows") or []:
            cells = r.get("cells") or []
            for ci, col in enumerate(cols):
                raw = cells[ci] if ci < len(cells) else ""
                yield {
                    "file": file_ref,
                    "table": t.get("name"),
                    "row": r.get("row"),
                    "column": col,
                    "index": ci,
                    "raw": raw,
                    "value": parse_number(raw),
                    "cell": cell_key(file_ref, t.get("name"), r.get("row"), col),
                    "format": t.get("format"),
                    "start": r.get("start"),
                    "end": r.get("end"),
                }


def iter_tables(dt):
    for t in (dt.get("tables") or []):
        yield t


def tables_of(dt, file_ref):
    want = norm_path(file_ref).lower()
    out = []
    for t in (dt.get("tables") or []):
        p = norm_path(t.get("file")).lower()
        if p == want or os.path.basename(p) == os.path.basename(want):
            out.append(t)
    return out


def _split_cell_key(cell):
    s = norm_path(cell)
    if "#" not in s:
        return None
    file_part, rest = s.split("#", 1)
    bits = rest.rsplit(":", 2)
    if len(bits) != 3:
        return None
    return file_part, bits[0], bits[1], bits[2]


def _file_matches(have, want):
    hp, wp = norm_path(have).lower(), norm_path(want).lower()
    if hp == wp or os.path.basename(hp) == os.path.basename(wp):
        return True
    return hp.lstrip("./") == wp.lstrip("./")


def _letters_to_index(s):
    n = 0
    for ch in s.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _col_matches(rec, col):
    want = str(col)
    if want == str(rec["column"]):
        return True
    m = _INDEX_HEADER.match(want)
    if m and int(m.group(1)) == rec["index"] + 1:
        return True
    m = _COL_LETTERS.match(want)
    if m and _letters_to_index(m.group(1)) == rec["index"]:
        return True
    return norm_label(want) == norm_label(rec["column"])


def resolve_cell(dt, cell=None, file=None, table=None, row=None, col=None):
    """Locate one cell by key or by coordinates. -> (record|None, reason, candidates).

    ``col`` may be a verbatim column NAME, a 1-based number or a spreadsheet LETTER.
    """
    if cell:
        parts = _split_cell_key(cell)
        if parts is None:
            return None, "cell key %r is not of the form file#table:row:col" % (cell,), []
        file, table, row, col = parts
    try:
        row_i = int(row) if row is not None and str(row) != "" else None
    except (TypeError, ValueError):
        return None, "row %r is not an integer" % (row,), []
    candidates = []
    for rec in iter_cells(dt):
        if file is not None and not _file_matches(rec["file"], file):
            continue
        if table not in (None, "") and str(table) != str(rec["table"]):
            continue
        if row_i is not None and int(rec["row"]) != row_i:
            continue
        if col is not None and not _col_matches(rec, col):
            continue
        candidates.append(rec)
    if not candidates:
        return None, "no cell matched", []
    if row_i is None and len({c["cell"] for c in candidates}) > 1:
        return None, "ambiguous: %d cells match the given coordinates" % len(candidates), candidates[:20]
    return candidates[0], "ok", candidates


def column_index(table, col):
    """Resolve ``col`` (verbatim name / 1-based number / letter) -> int|None."""
    cols = table.get("columns") or []
    want = str(col)
    if want in cols:
        return cols.index(want)
    m = _INDEX_HEADER.match(want)
    if m:
        i = int(m.group(1)) - 1
        return i if 0 <= i < len(cols) else None
    m = _COL_LETTERS.match(want)
    if m:
        i = _letters_to_index(m.group(1))
        return i if 0 <= i < len(cols) else None
    wl = norm_label(want)
    if not wl:
        return None
    hits = [i for i, c in enumerate(cols) if norm_label(c) == wl]
    if hits:
        return hits[0]
    # Looser match, scored: "all hint tokens are tokens of the column" beats a
    # plain substring, and a longer column name beats a shorter one.  Without
    # this the single-letter column "n" matched the hint "LDHA mRNA" as a
    # substring and made every series column resolve to None.
    want_tokens = wl.split()
    scored = []
    for i, c in enumerate(cols):
        cn = norm_label(c)
        if not cn:
            continue
        ct = cn.split()
        if all(t in ct for t in want_tokens):
            scored.append((2, len(cn), i))
        elif wl in cn:
            scored.append((1, len(cn), i))
        elif cn in wl:
            scored.append((0, len(cn), i))
    if not scored:
        return None
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    if len(scored) > 1 and scored[0][:2] == scored[1][:2]:
        return None                      # two equally good columns: not a resolution
    return scored[0][2]


def numeric_columns(table):
    out = []
    for ci in range(len(table.get("columns") or [])):
        if any(ci < len(r.get("cells") or []) and parse_number(r["cells"][ci]) is not None
               for r in (table.get("rows") or [])):
            out.append(ci)
    return out


def text_columns(table):
    num = set(numeric_columns(table))
    return [i for i in range(len(table.get("columns") or [])) if i not in num]


def table_by_name(dt, name, file_ref=None):
    for t in (dt.get("tables") or []):
        if str(t.get("name")) != str(name):
            continue
        if file_ref is None or _file_matches(t.get("file"), file_ref):
            return t
    return None


# --------------------------------------------------------------------------- #
# self-contained CLI (diagnostics only; chart.py drives the real pipeline)
# --------------------------------------------------------------------------- #
def load_materials(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="datamodel.py", description="build data-tables.json")
    p.add_argument("--materials")
    p.add_argument("--input")
    p.add_argument("--out", required=True)
    p.add_argument("--print-counts", action="store_true")
    args = p.parse_args(argv)
    mats = load_materials(args.materials) if args.materials else {}
    dt = build_tables(mats, args.input)
    d = os.path.dirname(os.path.abspath(args.out))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(dt, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    if args.print_counts:
        sys.stderr.write("[datamodel] %s\n" % json.dumps(dt["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

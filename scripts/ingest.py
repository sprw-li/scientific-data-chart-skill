#!/usr/bin/env python3
"""ingest.py — adaptive ingestion of an unknown ``/input`` layout.

Contract (work/CONTRACT.md §4)::

    ingest(input_dir, work_dir) -> {"case_id", "output_contract", "task_text",
                                   "materials": [...], "warnings": [...]}

Design notes
------------
* The case schema is UNKNOWN, so nothing is inferred from a file name.  Every
  file is classified by magic bytes + decoded content, and the extension is only
  a tie-breaker.
* Every material records ``sha256``, ``bytes``, the extracted ``text`` and a
  ``locators`` index: a list of ``{"locator", "start", "end", ...}`` entries
  whose ``start``/``end`` are CHARACTER offsets into ``text`` (and whose
  ``byte_start``/``byte_end`` are the UTF-8 byte offsets of the same range, so a
  quote can be located again on disk).  Locator syntax is chosen per kind:
  ``L12``/``L12-L18`` for text, ``row 14`` for CSV, ``p.3`` for PDF,
  ``$/a/b/0`` JSON pointers for JSON, ``Sheet1!B2`` for XLSX.
* Nothing is ever invented: an unreadable, empty, binary-only or unparseable
  file becomes an honest ``parsed: false`` record plus a warning.
* Fully deterministic: files are visited in sorted order, no random, no time.

The module has no mandatory third-party dependency.  PDF text is attempted with
pypdf / PyPDF2 / pdfminer.six / PyMuPDF when importable and otherwise falls back
to inflating FlateDecode streams from the raw bytes and reading ``BT``/``ET``
text-showing operators.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import csv
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
import zlib
import zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SCHEMA_VERSION = "1.0"

MAX_TEXT_CHARS = 900_000
MAX_PARSE_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGES = 400
MAX_JSON_NODES = 20000
MAX_TABLE_ROWS = 5000
MAX_TABLE_COLS = 256

TEXTUAL_EXTS = {
    ".md": "markdown", ".markdown": "markdown", ".txt": "text", ".text": "text",
    ".rst": "text", ".tex": "text", ".log": "text", ".yml": "text", ".yaml": "text",
    ".tsv": "tsv", ".tab": "tsv", ".csv": "csv", ".json": "json", ".jsonl": "jsonl",
    ".ndjson": "jsonl", ".xml": "text", ".html": "text", ".htm": "text", ".srt": "text",
}
BINARY_EXTS = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".bmp": "image", ".tif": "image", ".tiff": "image", ".webp": "image",
    ".pdf": "pdf", ".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xls-binary",
    ".docx": "zip-binary", ".pptx": "zip-binary", ".zip": "zip-binary",
    ".doc": "binary", ".ppt": "binary", ".exe": "binary", ".so": "binary",
    ".dll": "binary", ".bin": "binary", ".gz": "binary", ".tar": "binary",
}
MAGIC = (
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff", "image"),
    (b"GIF87a", "image"),
    (b"GIF89a", "image"),
    (b"BM", "image"),
    (b"II*\x00", "image"),
    (b"MM\x00*", "image"),
    (b"RIFF", "binary"),
    (b"\x1f\x8b", "binary"),
)

ROLE_ORDER = {"material": 0, "task": 1, "case": 2, "other": 3}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _utf8_offsets(text, start, end):
    """Byte range of ``text[start:end]`` inside the UTF-8 encoding of ``text``."""
    pre = text[:start].encode("utf-8")
    seg = text[start:end].encode("utf-8")
    return len(pre), len(pre) + len(seg)


def _walk_files(root):
    """All regular files under ``root`` (symlinks skipped), sorted by rel path."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in (".git", "__pycache__", ".svn", "node_modules"))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            if not os.path.isfile(full):
                continue
            out.append(_rel(full, root))
    out.sort()
    return out


def _warn(kind, subject, reason, needs_human=False):
    return {"kind": kind, "subject": subject, "reason": reason,
            "needs_human": bool(needs_human)}


def _locator(kind, text, start, end, extra=None):
    bs, be = _utf8_offsets(text, start, end)
    item = {"locator": kind, "start": int(start), "end": int(end),
            "byte_start": bs, "byte_end": be}
    if extra:
        item.update(extra)
    return item


# --------------------------------------------------------------------------- #
# locator indexes
# --------------------------------------------------------------------------- #
def _line_index(text):
    """``L1``.. locators; each spans one line without its terminator."""
    out = []
    pos = 0
    n = 0
    for line in text.split("\n"):
        n += 1
        out.append(("L%d" % n, pos, pos + len(line)))
        pos += len(line) + 1
    if out and out[-1][1] == out[-1][2] == len(text) and n > 1:
        out.pop()          # trailing newline -> drop the phantom empty last line
    return out


def _page_index(text):
    """``p.1``.. locators for text split on form feeds (PDF / PDF text dumps)."""
    out = []
    pos = 0
    pages = text.split("\f")
    for i, page in enumerate(pages):
        out.append(("p.%d" % (i + 1), pos, pos + len(page)))
        pos += len(page) + 1
    if out and out[-1][1] == out[-1][2] == len(text) and len(out) > 1:
        out.pop()
    return out


def _index_to_locators(pairs, text, value_of=None):
    out = []
    for loc, s, e in pairs:
        extra = {"value": value_of(s, e)} if value_of else None
        out.append(_locator(loc, text, s, e, extra))
    return out


# --------------------------------------------------------------------------- #
# decoding
# --------------------------------------------------------------------------- #
def _decode_text(data):
    """-> (text, encoding) or (None, reason).

    UTF-16 is checked before the binary heuristic AND before trusting a
    successful UTF-8 decode: ASCII UTF-16 is valid UTF-8 whose characters are
    interleaved with NULs, so a plain ".txt" saved by Windows Notepad used to be
    accepted as mojibake instead of being decoded.
    """
    if not data:
        return "", "empty"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        for enc in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                return data.decode(enc).replace("\r\n", "\n").replace("\r", "\n"), enc
            except UnicodeDecodeError:
                continue
    for enc in ("utf-8-sig", "utf-8"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            break
        if "\x00" not in text[:4096]:
            return text.replace("\r\n", "\n").replace("\r", "\n"), enc
        break                      # valid UTF-8 but NUL-laden -> probably UTF-16
    head = data[:4096]
    if head and head.count(b"\x00") / float(len(head)) > 0.25 or "\x00" in \
            _safe_decode(data[:4096], "utf-8", ""):
        for enc in ("utf-16-le", "utf-16-be", "utf-16"):
            try:
                text = data.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
            sample = text[:512]
            printable = sum(1 for ch in sample if ch.isprintable() or ch in "\n\t")
            if sample and printable / float(len(sample)) > 0.95:
                return text.replace("\r\n", "\n").replace("\r", "\n"), enc + " (no BOM)"
    if b"\x00" in head:
        return None, "nul-bytes"
    ctrl = sum(1 for b in head if b < 9 or 13 < b < 32)
    if head and ctrl / float(len(head)) > 0.10:
        return None, "binary-bytes"
    try:
        return data.decode("cp1252").replace("\r\n", "\n").replace("\r", "\n"), "cp1252"
    except UnicodeDecodeError:
        return None, "undecodable"


def _safe_decode(data, enc, default):
    try:
        return data.decode(enc)
    except Exception:  # noqa: BLE001
        return default


def _printable_ratio(text):
    if not text:
        return 0.0
    good = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    return good / float(len(text))


# --------------------------------------------------------------------------- #
# JSON: span-recording parser (no regex guessing, no third party)
# --------------------------------------------------------------------------- #
class JsonSpanParser:
    """Tiny recursive-descent JSON parser that records the span of every node."""

    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)
        self.nodes = []

    def error(self, msg):
        raise ValueError("json span parse: %s at %d" % (msg, self.i))

    def ws(self):
        while self.i < self.n and self.s[self.i] in " \t\r\n":
            self.i += 1

    def parse(self):
        self.ws()
        node = self.value("$", None)
        self.ws()
        return node

    def value(self, ptr, key):
        if len(self.nodes) >= MAX_JSON_NODES:
            self.error("too many nodes")
        self.ws()
        if self.i >= self.n:
            self.error("unexpected end")
        c = self.s[self.i]
        start = self.i
        if c == "{":
            return self.obj(ptr, key)
        if c == "[":
            return self.arr(ptr, key)
        value = self.scalar()
        node = {"ptr": ptr, "key": key, "type": type(value).__name__,
                "value": value, "start": start, "end": self.i,
                "vstart": start, "vend": self.i, "children": []}
        self.nodes.append(node)
        return node

    def scalar(self):
        c = self.s[self.i]
        if c == '"':
            return self.string()
        m = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?").match(self.s, self.i)
        if m:
            self.i = m.end()
            raw = m.group(0)
            return float(raw) if ("." in raw or "e" in raw or "E" in raw) else int(raw)
        for lit, val in (("true", True), ("false", False), ("null", None)):
            if self.s.startswith(lit, self.i):
                self.i += len(lit)
                return val
        self.error("bad scalar")

    def string(self):
        assert self.s[self.i] == '"'
        self.i += 1
        buf = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                self.i += 1
                esc = self.s[self.i]
                mapping = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
                           "n": "\n", "r": "\r", "t": "\t"}
                if esc == "u":
                    buf.append(chr(int(self.s[self.i + 1:self.i + 5], 16)))
                    self.i += 5
                    continue
                buf.append(mapping.get(esc, esc))
                self.i += 1
                continue
            if c == '"':
                self.i += 1
                return "".join(buf)
            buf.append(c)
            self.i += 1
        self.error("unterminated string")

    def obj(self, ptr, key):
        start = self.i
        self.i += 1
        obj = {}
        children = []
        self.ws()
        if self.i < self.n and self.s[self.i] == "}":
            self.i += 1
        else:
            while True:
                self.ws()
                kstart = self.i
                k = self.string()
                self.ws()
                if self.i >= self.n or self.s[self.i] != ":":
                    self.error("expected ':'")
                self.i += 1
                child = self.value(ptr + "/" + k.replace("~", "~0").replace("/", "~1"), k)
                child["kstart"], child["kend"] = kstart, kstart + len(k) + 2
                child["mstart"], child["mend"] = kstart, child["end"]
                obj[k] = child["value"]
                children.append(child)
                self.ws()
                if self.i < self.n and self.s[self.i] == ",":
                    self.i += 1
                    continue
                if self.i < self.n and self.s[self.i] == "}":
                    self.i += 1
                    break
                self.error("expected ',' or '}'")
        node = {"ptr": ptr, "key": key, "type": "object" if obj or True else "object",
                "value": obj, "start": start, "end": self.i,
                "vstart": start, "vend": self.i, "children": children}
        self.nodes.append(node)
        return node

    def arr(self, ptr, key):
        start = self.i
        self.i += 1
        items = []
        children = []
        self.ws()
        if self.i < self.n and self.s[self.i] == "]":
            self.i += 1
        else:
            idx = 0
            while True:
                child = self.value("%s/%d" % (ptr, idx), None)
                items.append(child["value"])
                children.append(child)
                idx += 1
                self.ws()
                if self.i < self.n and self.s[self.i] == ",":
                    self.i += 1
                    continue
                if self.i < self.n and self.s[self.i] == "]":
                    self.i += 1
                    break
                self.error("expected ',' or ']'")
        node = {"ptr": ptr, "key": key, "type": "array", "value": items,
                "start": start, "end": self.i, "vstart": start, "vend": self.i,
                "children": children}
        self.nodes.append(node)
        return node


def _json_nodes(text):
    """-> (nodes, error). ``nodes`` are span records in a stable JSON order."""
    try:
        parser = JsonSpanParser(text)
        root = parser.parse()
    except Exception as exc:  # noqa: BLE001 - honest parse failure
        try:
            json.loads(text)
        except Exception as exc2:  # noqa: BLE001
            return None, "invalid-json: %s" % exc2
        return None, "span-index-unavailable: %s" % exc
    if root is None:
        return [], None
    nodes = sorted(parser.nodes, key=lambda nd: (nd["start"], nd["end"]))
    return nodes, None


def _json_leaves(nodes):
    return [nd for nd in nodes if not nd["children"]]


# --------------------------------------------------------------------------- #
# CSV / TSV
# --------------------------------------------------------------------------- #
def _csv_raw_spans(text, delimiter):
    """Exact (start, end) of every CSV record in the RAW text.

    Splits on newlines that are outside a quoted field (doubled quotes escape),
    so a quoted field containing "," or a newline keeps one correct span.
    Deriving spans from ``delimiter.join(cells)`` instead silently mislocates
    every row after the first quoted field.
    """
    spans = []
    i, n, start, in_q = 0, len(text), 0, False
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


def _table_records(text, delimiter):
    """-> (header, header_row, rows, error).

    ``row N`` is the Nth record as ``csv.reader`` sees it, so the header is
    ``row 1`` and the first data record is ``row 2``.  Spans come from
    ``_csv_raw_spans`` (exact, quote-aware); blank records keep their number but
    emit no row and no span.
    """
    rows = []
    header = None
    header_row = None
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        records = list(reader)
    except Exception as exc:  # noqa: BLE001
        return [], None, [], "csv-error: %s" % exc
    spans = _csv_raw_spans(text, delimiter)
    note = None
    if len(spans) != len(records):
        note = ("csv-error: %d raw record(s) but the reader saw %d; spans may be "
                "approximate" % (len(spans), len(records)))
    n = 0
    for i, cells in enumerate(records):
        n += 1
        if i < len(spans):
            start, end = spans[i]
        else:
            start, end = (spans[-1][1] if spans else 0), (spans[-1][1] if spans else 0)
        if not cells and end <= start:
            continue                      # blank record: number consumed, nothing emitted
        record = {"locator": "row %d" % n, "start": start, "end": end,
                  "cells": list(cells), "n": n}
        if header is None:
            header = list(cells)
            header_row = record
        else:
            rows.append(record)
        if len(rows) > MAX_TABLE_ROWS:
            break
    if header is None:
        header = []
    return header, header_row, rows, note


def _numeric(cell):
    if cell is None:
        return None
    s = str(cell).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        m = re.match(r"^[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$", s)
        return float(m.group(0)) if m else None


def _table_numbers(header, rows, name):
    """Numeric column descriptors: [{"index","name","unit","value_count"}]."""
    cols = []
    width = 0
    for r in rows:
        width = max(width, len(r["cells"]))
    for ci in range(min(width, MAX_TABLE_COLS)):
        vals = []
        for r in rows:
            cells = r["cells"]
            if ci < len(cells):
                vals.append(_numeric(cells[ci]))
        good = [v for v in vals if v is not None]
        if len(good) >= 2 and len(good) >= 0.6 * max(1, len(vals)):
            label = header[ci] if ci < len(header) and header[ci] else "col%d" % (ci + 1)
            cols.append({"index": ci, "name": label, "unit": _unit_from_header(label),
                         "value_count": len(good), "table": name})
    return cols


_UNIT_HINT = re.compile(r"[\(\[]\s*([^\)\]]{1,24})\s*[\)\]]")


def _unit_from_header(label):
    m = _UNIT_HINT.search(label or "")
    if m:
        return m.group(1).strip()
    m2 = re.search(r"(?:^|_)(fold|pct|percent|pmol|nmol|mmol|umol|mpH|mmHg|min|sec|s|h|"
                   r"mg|ml|ul|ug|ng|pg|kDa|bp|nt|degC|K)$", (label or ""), re.I)
    if m2:
        return m2.group(1)
    return None


# --------------------------------------------------------------------------- #
# XLSX (openpyxl when available, else raw OOXML)
# --------------------------------------------------------------------------- #
def _xlsx_ns(tag):
    return tag.rsplit("}", 1)[-1]


def _xlsx_raw_rows(data):
    """Minimal OOXML reader -> {sheet_name: {ref: text}} in sheet order."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        return None, "not-a-zip: %s" % exc
    names = zf.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter() if _xlsx_ns(t.tag) == "t"))
    sheets = sorted(n for n in names
                    if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    out = []
    for si, sname in enumerate(sheets):
        root = ET.fromstring(zf.read(sname))
        cells = {}
        for c in root.iter():
            if _xlsx_ns(c.tag) != "c":
                continue
            ref = c.attrib.get("r") or ""
            t = c.attrib.get("t") or ""
            val = ""
            for child in c:
                if _xlsx_ns(child.tag) == "v":
                    val = child.text or ""
                elif _xlsx_ns(child.tag) == "is":
                    val = "".join(x.text or "" for x in child.iter() if _xlsx_ns(x.tag) == "t")
            if t == "s" and val.isdigit():
                idx = int(val)
                val = shared[idx] if 0 <= idx < len(shared) else val
            if ref:
                cells[ref] = val
        out.append({"name": "Sheet%d" % (si + 1), "path": sname, "cells": cells})
    return out, None


def _xlsx_rows_via_openpyxl(data):
    try:
        import openpyxl  # noqa: F401
    except Exception:  # noqa: BLE001
        return None, "openpyxl missing"
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        return None, "openpyxl: %s" % exc
    out = []
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append(["" if v is None else str(v) for v in row])
                if len(rows) > MAX_TABLE_ROWS:
                    break
            out.append({"name": ws.title, "rows": rows})
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
    return out, None


def _col_index(ref):
    m = re.match(r"^([A-Z]+)(\d+)$", ref or "")
    if not m:
        return None, None
    letters, row = m.group(1), int(m.group(2))
    ci = 0
    for ch in letters:
        ci = ci * 26 + (ord(ch) - 64)
    return ci - 1, row


def _rows_from_cells(cells):
    """OOXML cell refs -> a dense list of rows (list of cell strings)."""
    maxrow = 0
    maxcol = 0
    grid = {}
    for ref, val in (cells or {}).items():
        ci, ri = _col_index(ref)
        if ri is None:
            continue
        grid[(ri, ci)] = "" if val is None else str(val)
        maxrow = max(maxrow, ri)
        maxcol = max(maxcol, ci)
    rows = []
    for r in range(1, maxrow + 1):
        rows.append([grid.get((r, c), "") for c in range(maxcol + 1)])
    return rows


def _sheet_rows(sheets):
    """Normalise openpyxl / OOXML sheets to ``[{"name","rows"}]``."""
    out = []
    for sheet in sheets:
        if "rows" in sheet:
            rows = [[("" if v is None else str(v)) for v in row] for row in sheet["rows"]]
        else:
            rows = _rows_from_cells(sheet.get("cells"))
        if rows:
            out.append({"name": sheet.get("name") or "Sheet1", "rows": rows})
    return out


def _xlsx_rows_text(sheets):
    """-> (text, locators, tables) built from dense workbook rows."""
    lines = []
    spans = []           # (locator, start, end, kind) with local start offsets
    tables = []
    pos = 0
    for sheet in sheets:
        name = sheet["name"]
        rows = sheet["rows"]
        header = list(rows[0]) if rows else []
        tbl_rows = []
        for ri, vals in enumerate(rows, start=1):
            line = "%s!row %d\t%s" % (name, ri, "\t".join(vals))
            start = pos
            spans.append(("%s!row %d" % (name, ri), start, start + len(line)))
            for c, val in enumerate(vals):
                if val != "":
                    spans.append(("%s%s%d" % (name, _col_letters(c), ri), start,
                                  start + len(line)))
            if ri > 1:
                tbl_rows.append({"locator": "%s!row %d" % (name, ri), "start": start,
                                 "end": start + len(line), "cells": vals, "n": ri})
            lines.append(line)
            pos += len(line) + 1
        tables.append({"name": name, "header": header, "rows": tbl_rows, "source": "xlsx"})
    text = "\n".join(lines)
    locators = [_locator(loc, text, s, e) for loc, s, e in spans]
    return text, locators, tables


def _col_letters(ci):
    s = ""
    n = ci + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
_PDF_EXTRACTORS = []


def _try_pypdf(data):
    try:
        import pypdf
    except Exception:  # noqa: BLE001
        return None
    reader = pypdf.PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    return pages


def _try_pypdf2(data):
    try:
        import PyPDF2
    except Exception:  # noqa: BLE001
        return None
    reader = PyPDF2.PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    return pages


def _try_pdfminer(data):
    try:
        from pdfminer.high_level import extract_text
    except Exception:  # noqa: BLE001
        return None
    text = extract_text(io.BytesIO(data), maxpages=MAX_PDF_PAGES) or ""
    return text.split("\f")


def _try_fitz(data):
    try:
        import fitz
    except Exception:  # noqa: BLE001
        return None
    doc = fitz.open(stream=data, filetype="pdf")
    pages = []
    for page in doc:
        try:
            pages.append(page.get_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    doc.close()
    return pages


_PDF_EXTRACTORS = [
    ("pypdf", _try_pypdf), ("PyPDF2", _try_pypdf2),
    ("pdfminer.six", _try_pdfminer), ("PyMuPDF", _try_fitz),
]


def _inflate(raw):
    for wbits in (15, -15, 47):
        try:
            return zlib.decompress(raw, wbits)
        except Exception:  # noqa: BLE001
            continue
    try:
        return zlib.decompressobj().decompress(raw)
    except Exception:  # noqa: BLE001
        return None


_PDF_TOKEN = re.compile(
    r"\((?:\\.|[^\\()])*\)"          # literal string
    r"|<[0-9A-Fa-f\s]*>"             # hex string
    r"|T\*|Td|TD|TJ|Tj|'|\""         # operators
    r"|\bET\b", re.S)


def _pdf_string_body(tok):
    body = tok[1:-1]
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            break
        esc = body[i]
        simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
                  "(": "(", ")": ")", "\\": "\\"}
        if esc in simple:
            out.append(simple[esc])
            i += 1
        elif esc.isdigit():
            m = re.match(r"[0-7]{1,3}", body[i:])
            out.append(chr(int(m.group(0), 8)))
            i += len(m.group(0))
        elif esc == "\n":
            i += 1
        else:
            out.append(esc)
            i += 1
    return "".join(out)


def _content_to_text(content):
    """Pull text out of one PDF content stream's BT/ET blocks."""
    try:
        txt = content.decode("latin-1")
    except Exception:  # noqa: BLE001
        return ""
    pieces = []
    for block in re.finditer(r"\bBT\b(.*?)\bET\b", txt, re.S):
        buf = []
        for m in _PDF_TOKEN.finditer(block.group(1)):
            tok = m.group(0)
            if tok.startswith("("):
                buf.append(_pdf_string_body(tok))
            elif tok.startswith("<"):
                hx = re.sub(r"\s+", "", tok[1:-1])
                if len(hx) % 2:
                    hx += "0"
                try:
                    raw = bytes.fromhex(hx)
                except ValueError:
                    continue
                if raw[:2] in (b"\xfe\xff",):
                    buf.append(raw.decode("utf-16-be", "replace"))
                else:
                    buf.append(raw.decode("latin-1", "replace"))
            elif tok in ("Td", "TD", "T*", "'", '"', "ET"):
                buf.append("\n")
            elif tok in ("TJ", "Tj"):
                buf.append(" ")
        chunk = "".join(buf)
        if chunk.strip():
            pieces.append(chunk)
    text = "\n".join(pieces)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _pdf_pages_raw(data):
    """Fallback: inflate FlateDecode streams and read BT/ET text operators."""
    pages = []
    for m in re.finditer(rb"stream\r?\n", data, re.S):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        raw = data[start:end]
        raw = raw.rstrip(b"\r\n")
        content = _inflate(raw)
        if content is None:
            content = raw
        if b"BT" not in content:
            continue
        txt = _content_to_text(content)
        if txt.strip():
            pages.append(txt)
        if len(pages) >= MAX_PDF_PAGES:
            break
    if not pages:
        txt = _content_to_text(data)
        if txt.strip():
            pages = [txt]
    return pages


def _extract_pdf(data):
    """-> (pages, method, reason). ``pages`` is a list of page text strings."""
    methods = []
    for name, fn in _PDF_EXTRACTORS:
        try:
            pages = fn(data)
        except Exception as exc:  # noqa: BLE001
            methods.append("%s-failed(%s)" % (name, type(exc).__name__))
            continue
        if pages is None:
            methods.append("%s-unavailable" % name)
            continue
        joined = "".join(pages)
        if joined.strip():
            return pages, name, "ok (%s)" % name
        methods.append("%s-empty" % name)
    pages = _pdf_pages_raw(data)
    if pages and "".join(pages).strip():
        return pages, "raw-flate-bt-et", "ok (raw stream fallback)"
    return [], "none", "no extractable text; tried: %s" % ", ".join(methods or ["nothing"])


# --------------------------------------------------------------------------- #
# per-material extraction
# --------------------------------------------------------------------------- #
def _material_base(rel, role, data):
    base = {"path": rel, "role": role, "bytes": len(data),
            "sha256": _sha256_bytes(data), "parsed": False, "parse_reason": "",
            "kind": None, "text": "", "pages": [], "locator_scheme": None,
            "locators": [], "tables": [], "nodes": [], "warnings": [],
            "encoding": None, "truncated": False,
            # `locators[].start/end` are CHARACTER offsets into `text`;
            # `byte_start/byte_end` are UTF-8 byte offsets into the SAME decoded
            # text (newlines normalised to \n), NOT into the raw file on disk.
            "offsets_basis": "decoded text (utf-8, newlines normalised to \\n)"}
    return base


def _finalise_text(mat, text, scheme, locators, text_extra=None):
    if len(text) > MAX_TEXT_CHARS:
        cut = text.rfind("\n", 0, MAX_TEXT_CHARS)
        cut = cut if cut > 1000 else MAX_TEXT_CHARS
        text = text[:cut]
        mat["truncated"] = True
        mat["warnings"].append(_warn("truncated", mat["path"],
                                     "text truncated at %d characters; evidence beyond that "
                                     "offset cannot be quoted" % cut))
    mat["text"] = text
    mat["locator_scheme"] = scheme
    mat["locators"] = [loc for loc in locators if loc["end"] <= len(text)]
    if text_extra:
        mat.update(text_extra)


def _extract_material(rel, role, data):
    mat = _material_base(rel, role, data)
    ext = os.path.splitext(rel)[1].lower()
    text, enc = _decode_text(data)
    magic = None
    for sig, kind in MAGIC:
        if data.startswith(sig):
            magic = kind
            break
    if data == b"":
        mat["kind"] = "empty"
        mat["parse_reason"] = "zero-length file"
        mat["warnings"].append(_warn("empty_file", rel, "file is 0 bytes; nothing to extract"))
        return mat
    if len(data) > MAX_PARSE_BYTES:
        mat["kind"] = BINARY_EXTS.get(ext, "binary")
        mat["parse_reason"] = "file exceeds %d bytes" % MAX_PARSE_BYTES
        mat["warnings"].append(_warn("too_large", rel, mat["parse_reason"]))
        return mat

    if text is not None:
        # record how the bytes were decoded: a quote is only "verbatim" with
        # respect to THIS decoding, so the encoding is part of the provenance
        mat["encoding"] = enc

    # --- PDF ------------------------------------------------------------- #
    if magic == "pdf" or ext == ".pdf":
        mat["kind"] = "pdf"
        pages, method, reason = _extract_pdf(data)
        mat["pdf_extractor"] = method
        if not pages:
            mat["parse_reason"] = reason
            mat["warnings"].append(_warn("pdf_unparsed", rel, reason, needs_human=True))
            return mat
        text = "\f".join(pages)
        pairs = _page_index(text)
        locs = _index_to_locators(pairs, text)
        _finalise_text(mat, text, "page", locs)
        mat["encoding"] = "pdf-text-layer"
        mat["pages"] = [{"page": i + 1, "locator": "p.%d" % (i + 1),
                         "chars": len(p)} for i, p in enumerate(pages)]
        mat["parsed"] = True
        mat["parse_reason"] = reason
        ratio = _printable_ratio(text)
        alnum = sum(1 for ch in text if ch.isalnum()) / float(max(1, len(text)))
        if alnum < 0.20 or ratio < 0.80:
            mat["warnings"].append(_warn(
                "low_text_quality", rel,
                "extracted PDF text looks garbled (alnum=%.2f printable=%.2f); "
                "quotes below are still verbatim substrings of the extraction" % (alnum, ratio)))
        return mat

    # --- XLSX ------------------------------------------------------------ #
    if ext in (".xlsx", ".xlsm"):
        mat["kind"] = "xlsx"
        sheets, err = _xlsx_rows_via_openpyxl(data)
        method = "openpyxl"
        if sheets is None:
            sheets, err = _xlsx_raw_rows(data)
            method = "ooxml-raw"
        if sheets is None:
            mat["kind"] = "xls-binary" if ext == ".xls" else "xlsx-unreadable"
            mat["parse_reason"] = "workbook could not be read: %s" % err
            mat["warnings"].append(_warn("xlsx_unparsed", rel, mat["parse_reason"], True))
            return mat
        dense = _sheet_rows(sheets)
        if not dense:
            mat["parse_reason"] = "workbook contains no non-empty sheet"
            mat["warnings"].append(_warn("xlsx_empty", rel, mat["parse_reason"]))
            return mat
        text, locs, tables = _xlsx_rows_text(dense)
        _finalise_text(mat, text, "cell", locs,
                       {"tables": tables, "xlsx_reader": method})
        mat["parsed"] = True
        mat["parse_reason"] = "ok (%s, %d sheet(s))" % (method, len(dense))
        return mat

    # --- image / other binary -------------------------------------------- #
    if magic == "image" or ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"):
        mat["kind"] = "image"
        mat["parse_reason"] = "raster image: no text layer available to this runtime"
        mat["warnings"].append(_warn(
            "image_no_text", rel,
            "raster image; this build performs no OCR, so it contributes no text "
            "or evidence (metadata only)", needs_human=False))
        return mat

    if text is None:
        declared = BINARY_EXTS.get(ext)
        mat["kind"] = declared if declared and declared not in ("zip",) else "binary"
        if ext == ".xls":
            mat["kind"] = "xls-binary"
        mat["parse_reason"] = "not decodable as text (%s)" % enc
        mat["warnings"].append(_warn(
            "binary_only", rel,
            "binary file that this build cannot parse (%s); recorded by sha256 only" % enc))
        return mat

    reason = None
    # --- JSON ------------------------------------------------------------ #
    sniffed_json = text.lstrip()[:1] in ("{", "[")
    if ext in (".json",) or (sniffed_json and ext not in (".csv", ".tsv")):
        nodes, err = None, None
        if sniffed_json:
            nodes, err = _json_nodes(text)
        if nodes is not None:
            mat["kind"] = "json"
            locs = []
            for nd in nodes:
                name = nd["ptr"]
                if nd["children"]:
                    locs.append(_locator(name, text, nd["start"], nd["end"]))
                else:
                    locs.append(_locator(name, text, nd["vstart"], nd["vend"],
                                         {"value": nd["value"]}))
            # also expose member-level locators (key: value) for nicer quotes
            for nd in nodes:
                if nd.get("mstart") is not None:
                    locs.append(_locator(nd["ptr"] + "#member", text, nd["mstart"], nd["mend"]))
            locs.sort(key=lambda l: (l["start"], l["end"]))
            _finalise_text(mat, text, "json-pointer", locs, {"nodes": [
                {"ptr": nd["ptr"], "type": nd["type"], "start": nd["start"], "end": nd["end"],
                 "vstart": nd["vstart"], "vend": nd["vend"], "key": nd["key"],
                 "value": nd["value"] if not nd["children"] else None}
                for nd in nodes]})
            mat["parsed"] = True
            mat["parse_reason"] = "ok (json, %d node(s))" % len(nodes)
            mat["kind"] = "json"
            return mat
        if ext == ".json":
            mat["kind"] = "text"
            mat["parse_reason"] = "declared .json but %s" % (err or "not JSON")
            mat["warnings"].append(_warn("json_invalid", rel, mat["parse_reason"]))
            pairs = _line_index(text)
            _finalise_text(mat, text, "line", _index_to_locators(pairs, text))
            mat["parsed"] = True
            return mat

    # --- CSV / TSV ------------------------------------------------------- #
    delim = "\t" if ext in (".tsv", ".tab") else ","
    looks_delimited = False
    head_lines = [l for l in text.split("\n")[:20] if l.strip()]
    if head_lines:
        counts = [l.count(delim) for l in head_lines]
        looks_delimited = counts[0] >= 1 and all(c == counts[0] for c in counts[:min(10, len(counts))])
    if ext in (".csv", ".tsv", ".tab") or (looks_delimited and ext not in (".md", ".markdown")):
        header, header_row, rows, err = _table_records(text, delim)
        if err:
            mat["warnings"].append(_warn("csv_error", rel, err))
        width = len(header)
        ragged = [r["locator"] for r in rows if len(r["cells"]) != width]
        if ragged and width:
            mat["warnings"].append(_warn(
                "ragged_rows", rel,
                "%d of %d record(s) have a different field count than the header (%d): %s; "
                "short rows are kept as-is and no cell is invented"
                % (len(ragged), len(rows), width, ", ".join(ragged[:5]))))
        # the header is exactly `row 1`, so the header labels are quotable too
        pairs = ([(header_row["locator"], header_row["start"], header_row["end"])]
                 if header_row else []) + [(r["locator"], r["start"], r["end"]) for r in rows]
        locs = _index_to_locators(pairs, text)
        value_of = {}
        if header_row:
            value_of[header_row["locator"]] = header_row["cells"]
        for r in rows:
            value_of[r["locator"]] = r["cells"]
        for loc in locs:
            loc["value"] = value_of.get(loc["locator"])
        mat["kind"] = "tsv" if delim == "\t" else "csv"
        _finalise_text(mat, text, "row", locs,
                       {"tables": [{"name": os.path.basename(rel), "header": header,
                                    "rows": rows, "source": "csv", "delimiter": delim,
                                    "header_row": header_row}]})
        mat["parsed"] = True
        mat["parse_reason"] = "ok (%d row(s), %d column(s))" % (len(rows), len(header))
        return mat

    # --- markdown / text / pdf text dump --------------------------------- #
    scheme = "line"
    pairs = _line_index(text)
    locs = _index_to_locators(pairs, text)
    if "\f" in text:
        page_pairs = _page_index(text)
        if len(page_pairs) > 1:
            for loc, s, e in page_pairs:
                locs.append(_locator(loc, text, s, e))
            locs.sort(key=lambda l: (l["start"], l["end"]))
    kind = TEXTUAL_EXTS.get(ext, "text")
    if kind in ("text",) and re.search(r"\f|^\s*---\s*page\s+\d+", text, re.I | re.M):
        kind = "pdf-text"
        mat["looks_like_pdf_text"] = True
    if kind in ("text", "pdf-text") and re.search(r"^#{1,6}\s|^\s*[-*]\s+\S|^\s*\d+\.\s+\S",
                                                  text, re.M):
        kind = "markdown" if not mat.get("looks_like_pdf_text") else kind
    mat["kind"] = kind
    _finalise_text(mat, text, scheme, locs)
    mat["parsed"] = bool(text.strip())
    if not text.strip():
        mat["parse_reason"] = "decoded but contains no text"
        mat["warnings"].append(_warn("no_text", rel, "file decoded but has no text content"))
    else:
        mat["parse_reason"] = "ok (text, %d line(s))" % len(pairs)
    return mat


# --------------------------------------------------------------------------- #
# case.json
# --------------------------------------------------------------------------- #
_CASE_ID_KEYS = ("case_id", "caseId", "id", "name", "title", "case", "case_name")


def _case_id_from(case, fallback):
    if isinstance(case, dict):
        for k in _CASE_ID_KEYS:
            v = case.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                for k2 in ("id", "case_id", "name"):
                    v2 = v.get(k2)
                    if isinstance(v2, str) and v2.strip():
                        return v2.strip()
    return fallback


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def ingest(input_dir, work_dir):
    """Read ``input_dir`` and materialise everything later stages need.

    Returns a dict with ``case_id``, ``output_contract``, ``task_text``,
    ``materials`` (each carrying text + locators + hashes) and ``warnings``.
    Raises ``SystemExit(2)`` when the input directory does not exist (contract
    §5: 2 = usage/IO).
    """
    input_dir = os.path.abspath(input_dir)
    if not os.path.isdir(input_dir):
        sys.stderr.write("[ingest] input directory not found: %s\n" % input_dir)
        raise SystemExit(2)

    warnings = []
    rels = _walk_files(input_dir)
    if not rels:
        sys.stderr.write("[ingest] input directory is empty: %s\n" % input_dir)
        raise SystemExit(2)

    materials = []
    for rel in rels:
        role = "case" if rel == "case.json" else ("task" if rel == "task.md" else "material")
        full = os.path.join(input_dir, rel.replace("/", os.sep))
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            warnings.append(_warn("unreadable", rel, "cannot read file: %s" % exc, True))
            materials.append({"path": rel, "role": role, "bytes": None, "sha256": None,
                              "kind": "unreadable", "parsed": False,
                              "parse_reason": "unreadable: %s" % exc, "text": "",
                              "locators": [], "locator_scheme": None, "pages": [],
                              "tables": [], "nodes": [], "warnings": [
                                  _warn("unreadable", rel, "cannot read file: %s" % exc, True)]})
            continue
        mat = _extract_material(rel, role, data)
        warnings.extend(mat.get("warnings") or [])
        materials.append(mat)

    materials.sort(key=lambda m: (ROLE_ORDER.get(m.get("role"), 9), m.get("path") or ""))

    # case.json
    case = {}
    case_path = os.path.join(input_dir, "case.json")
    case_sha = None
    if os.path.exists(case_path):
        try:
            with open(case_path, "rb") as fh:
                cdata = fh.read()
            case_sha = _sha256_bytes(cdata)
            case = json.loads(cdata.decode("utf-8-sig"))
            if not isinstance(case, dict):
                warnings.append(_warn("case_json_shape", "case.json",
                                      "case.json is not a JSON object (%s)" % type(case).__name__))
                case = {}
        except Exception as exc:  # noqa: BLE001
            warnings.append(_warn("case_json_invalid", "case.json",
                                  "case.json could not be parsed: %s" % exc, True))
    else:
        warnings.append(_warn("case_json_missing", "case.json",
                              "no case.json found in the input directory; case_id falls back "
                              "to the directory name", True))

    contract = case.get("output_contract") if isinstance(case.get("output_contract"), dict) else {}
    if not contract and case:
        warnings.append(_warn("output_contract_missing", "case.json",
                              "case.json has no output_contract object; result.json will carry "
                              "the standard fields only", False))

    fallback_id = os.path.basename(os.path.normpath(input_dir)) or "unknown-case"
    case_id = _case_id_from(case, fallback_id)

    task_text = ""
    for m in materials:
        if m.get("role") == "task" and m.get("text"):
            task_text = m["text"]
            break
    if not task_text:
        warnings.append(_warn("task_md_missing", "task.md",
                              "no readable task.md in the input directory", True))

    scientific = [m for m in materials if m.get("role") == "material"]
    parsed_sci = [m for m in scientific if m.get("parsed") and (m.get("text") or "").strip()]
    if not scientific:
        warnings.append(_warn("no_materials", "materials/",
                              "no material files were supplied; the figure can only quote the "
                              "task text and must stay free of scientific claims", True))
    elif not parsed_sci:
        warnings.append(_warn("no_parsable_materials", "materials/",
                              "none of the %d material file(s) yielded readable text"
                              % len(scientific), True))

    out = {
        "schema_version": SCHEMA_VERSION,
        "input_dir": input_dir,
        "work_dir": os.path.abspath(work_dir) if work_dir else None,
        "case_id": case_id,
        "case_id_source": "case.json" if case and case_id != fallback_id else "input-dir-name",
        "output_contract": contract,
        "case_json_path": case_path if os.path.exists(case_path) else None,
        "case_json_sha256": case_sha,
        "case_keys": sorted(case.keys()) if isinstance(case, dict) else [],
        "task_text": task_text,
        "input_files": rels,
        "materials": materials,
        "counts": {
            "files": len(materials),
            "materials": len(scientific),
            "parsed": len([m for m in materials if m.get("parsed")]),
            "parsed_materials": len(parsed_sci),
        },
        "parsable": bool(parsed_sci),
        "warnings": warnings,
    }
    return out


def main(argv=None):
    """Convenience entry point: ``python ingest.py <input_dir> [work_dir]``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: ingest.py <input_dir> [work_dir]\n")
        return 2
    inp = argv[0]
    work = argv[1] if len(argv) > 1 else os.path.join(os.getcwd(), "work")
    result = ingest(inp, work)
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "materials.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    sys.stderr.write("[ingest] %d file(s), %d material(s), %d warning(s)\n"
                     % (result["counts"]["files"], result["counts"]["materials"],
                        len(result["warnings"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())

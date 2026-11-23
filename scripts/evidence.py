#!/usr/bin/env python3
"""evidence.py — the evidence ledger and the provenance gate (contract §4, §6E).

Every entity, relation and number that may reach the figure becomes an
*evidence item*: a claim plus a locator plus a **verbatim** quote from a real
material file.  Nothing in this module invents content: if a claim cannot be tied
to a span of text that actually exists, it is not emitted (and the caller records
the uncertainty instead).

Locator schemes produced by `ingest.py` (this module accepts all of them):
    line     "L12", "L12-L14"      markdown / plain text / pdf text dumps
    page     "p.3"                 form-feed paginated text
    row      "row 9"               CSV/TSV records (header is row 1)
    json     "json:<pointer>"      JSON leaves
Quotes are matched after whitespace normalisation, so a quote that spans a line
break in the file still resolves.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import json
import os
import re

MAX_ITEMS = 60
MAX_QUOTE = 320
MAX_TABLE_ROWS = 24

_WS = re.compile(r"\s+")
# A measurable number: not glued to a letter on either side, so identifiers such
# as "HIF-1alpha", "Ser293", "H9c2" and "PDK1" never turn into data points.
_NUM = re.compile(r"(?<![A-Za-z0-9._\-])-?\d+(?:\.\d+)?(?![A-Za-z])")
_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+([^\n]*)$", re.M)
# PDF text has no markdown markers, so a section word sits at the start of the
# sentence that follows it ("Results Hypoxia raised ..."). Treating it as part of
# the claim pushes the section name into every label built from that sentence.
_SECTION_WORD = re.compile(
    r"^(abstract|introduction|background|methods?|materials?|results?|findings|"
    r"discussion|conclusions?|summary|outlook|figure\s*\d+|table\s*\d+)"
    r"[ \t]*[:.\u2014-]?[ \t\r\n]+", re.I)
# A sentence ends at terminal punctuation FOLLOWED BY whitespace/end, or at a
# blank line. Requiring the boundary keeps "2.8-fold" and "Fig. 3" intact --
# splitting on every "." silently turned every decimal into a truncated claim.
_SENT_END = re.compile(r"(?<=[.!?;\u3002\uff01\uff1f\uff1b])(?=\s|$)|\n\s*\n")

# Transitive relation verbs. Intransitive motion verbs ("rise", "fall") are
# deliberately absent: "a fall in oxygen availability" is a change, not an edge
# from one entity to another, and treating it as one is how figures end up with
# arrows pointing the wrong way.
_TRANS_INH = (
    "inhibits", "inhibit", "inhibited", "suppresses", "suppress", "suppressed", "reduces",
    "reduce", "reduced", "lowers", "lower", "lowered", "decreases", "decrease", "decreased",
    "abolished", "abolish", "abolishes", "blocks", "block", "blocked", "restrains", "restrain",
    "restrained", "prevents", "prevent", "prevented", "represses", "repress", "repressed",
    "\u6291\u5236", "\u4e0b\u8c03", "\u964d\u4f4e", "\u963b\u65ad", "\u51cf\u5f31",
)
_TRANS_ACT = (
    "activates", "activate", "activated", "increases", "increase", "increased", "raises",
    "raise", "raised", "upregulates", "upregulate", "upregulated", "induces", "induce",
    "induced", "drives", "drive", "drove", "promotes", "promote", "promoted", "stabilises",
    "stabilizes", "stabilised", "stabilized", "phosphorylates", "phosphorylate",
    "phosphorylated", "enhances", "enhance", "enhanced", "maintains", "maintain", "maintained",
    "restores", "restore", "restored", "requires", "require", "required",
    "reprograms", "reprogram", "reprogrammed", "sustains", "sustain", "sustained",
    "\u6fc0\u6d3b", "\u4e0a\u8c03", "\u4fc3\u8fdb", "\u589e\u52a0", "\u5347\u9ad8",
)
_VERB2KIND = {}
for _v in _TRANS_INH:
    _VERB2KIND[_v] = "inhibition"
for _v in _TRANS_ACT:
    _VERB2KIND[_v] = "activation"

# subject/object token: capitalised names, gene/protein-like tokens, hyphenated
# compounds and short lowercase nouns, optionally 1-3 words.
_TERM = (r"(?:[A-Za-z\u03b1-\u03c9][A-Za-z0-9\u03b1-\u03c9\-]*(?:\s+[A-Za-z0-9\u03b1-\u03c9\-]+){0,2})")
_REL_RE = re.compile(
    r"(?P<subj>" + _TERM + r")\s+(?:also\s+|then\s+)?(?P<verb>" + "|".join(
        sorted(_VERB2KIND, key=len, reverse=True)) + r")\s+(?P<obj>" + _TERM + r")",
    re.I,
)
_ARROW_RE = re.compile(r"(?P<subj>" + _TERM + r")\s*(?:\u2192|->|\u27f6)\s*(?P<obj>" + _TERM + r")")

_STOP_TERMS = {
    "the", "a", "an", "this", "these", "those", "we", "it", "they", "its", "their", "and",
    "but", "which", "that", "cells", "cell", "figure", "table", "data", "values", "results",
    "here", "both", "all", "each", "these", "reported", "measured",
    "is", "are", "was", "were", "be", "been", "being", "what", "who", "whom", "whose",
    "of", "to", "in", "on", "at", "by", "for", "from", "with", "without", "or", "not",
    "then", "also", "thus", "hence", "however", "while", "when", "where",
}

# unit phrase -> canonical unit (longest match first)
UNITS = [
    ("milli-pH units per minute", "mpH/min"),
    ("milli-pH unit per minute", "mpH/min"),
    ("nanomoles per milligram", "nmol/mg"),
    ("nmol per milligram", "nmol/mg"),
    ("pmol per minute", "pmol/min"),
    ("pmol/min", "pmol/min"),
    ("units per minute", "AU/min"),
    ("per second", "per second"),
    ("fold", "fold"),
    ("percent", "percent"),
    ("percentage points", "pp"),
    ("nmol/mg", "nmol/mg"),
    ("mmol/L", "mmol/L"),
    ("\u00b5M", "uM"),
    ("uM", "uM"),
    ("nM", "nM"),
    ("mM", "mM"),
    ("mg/mL", "mg/mL"),
    ("\u00b0C", "degC"),
    ("hours", "h"),
    ("hour", "h"),
    ("minutes", "min"),
    ("seconds", "s"),
    ("cells", "cells"),
    ("per mg", "per mg"),
    ("%", "percent"),
]

_METHOD = (
    "measured", "quantified", "assayed", "measured by", "exposed to", "performed", "using",
    "cultured", "transfected", "incubated", "stained", "n =", "n=", "normalised", "normalized",
)


def _norm(text):
    return _WS.sub(" ", text or "").strip()


def _clip(text, limit=MAX_QUOTE):
    """Return a verbatim prefix of ``text``, cut on a word/sentence boundary."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > limit // 2 else limit]


# --------------------------------------------------------------------------- #
# sentence segmentation (spans over the ORIGINAL text so quotes stay verbatim)
# --------------------------------------------------------------------------- #
def sentences(text):
    """-> [{"start","end","text","heading","section"}] in document order."""
    out = []
    section = ""
    if not text:
        return out
    bounds = []
    for m in _SENT_END.finditer(text):
        end = m.end()
        if end == m.start():                      # zero-width terminal-punctuation cut
            while end < len(text) and text[end] in " \t":
                end += 1
        if not bounds or end > bounds[-1]:
            bounds.append(end)
    if not bounds or bounds[-1] < len(text):
        bounds.append(len(text))

    pos = 0
    for end in bounds:
        if end <= pos:
            continue
        chunk = text[pos:end]
        stripped = chunk.strip()
        if stripped:
            hm = _HEADING.match(stripped)
            if hm:
                level = len(hm.group(1))
                section = _norm(hm.group(2))
                if level >= 2:
                    out.append({"start": pos, "end": pos + len(hm.group(0).lstrip()),
                                "text": hm.group(2).strip(), "heading": True, "section": section})
                # the heading and the sentence glued to it are different units:
                # otherwise "Background" becomes a prefix of the first claim
                nl = chunk.find("\n")
                rest_start = nl + 1
                if nl >= 0 and chunk[rest_start:].strip():
                    rest = chunk[rest_start:]
                    out.append({"start": pos + rest_start, "end": end, "text": rest,
                                "heading": False, "section": section})
            else:
                out.append({"start": pos, "end": end, "text": chunk, "heading": False,
                            "section": section})
        pos = end
    # split a leading plain-text section word off each unit ("Results Hypoxia ...")
    split = []
    for unit in out:
        if unit.get("heading"):
            split.append(unit)
            continue
        text_u = unit["text"]
        m = _SECTION_WORD.match(text_u.lstrip())
        if not m:
            split.append(unit)
            continue
        lead = len(text_u) - len(text_u.lstrip())
        head_start = unit["start"] + lead
        rest_start = unit["start"] + lead + m.end()
        section = _norm(m.group(1))
        split.append({"start": head_start, "end": head_start + m.end(), "text": m.group(1).strip(),
                      "heading": True, "section": section})
        if text_u[m.end():].strip():
            split.append({"start": rest_start, "end": unit["end"], "text": text_u[m.end():],
                          "heading": False, "section": section})
    return split


# --------------------------------------------------------------------------- #
# content classifiers
# --------------------------------------------------------------------------- #
def _unit_for(text, value_end):
    """Look for a unit phrase anywhere in the sentence; prefer one after the number.

    Matching is word-bounded: without it "uM" is found inside "cons*um*ption" and
    a respiratory rate silently acquires the wrong unit.
    """
    tail = text[value_end:value_end + 80]
    head = text
    for hay in (tail, head):
        for phrase, unit in UNITS:
            if re.search(r"(?<![A-Za-z])" + re.escape(phrase) + r"(?![A-Za-z])", hay, re.I):
                return unit
    return None


def last_number(text):
    """-> (value, unit) of the LAST number in the text, or (None, None)."""
    last = None
    for m in _NUM.finditer(text):
        last = m
    if last is None:
        return None, None
    try:
        value = float(last.group(0))
    except ValueError:
        return None, None
    return value, _unit_for(text, last.end())


def all_numbers(text):
    out = []
    for m in _NUM.finditer(text):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            pass
    return out


def relation_kind(text):
    """Direction of a *transitive* relation, or None. See _TRANS_* above."""
    for m in _REL_RE.finditer(text):
        verb = m.group("verb").lower()
        kind = _VERB2KIND.get(verb) or _VERB2KIND.get(verb.rstrip("s"))
        if kind and _clean_term(m.group("subj")) and _clean_term(m.group("obj")):
            return kind
    if _ARROW_RE.search(text):
        return "activation"
    return None


def _clean_term(term):
    t = _norm(term)
    if not t:
        return None
    words = [w for w in t.split(" ") if w]
    while words and words[0].lower() in _STOP_TERMS:
        words.pop(0)
    while words and words[-1].lower() in _STOP_TERMS:
        words.pop()
    while words and words[-1].lower() in _VERB2KIND:      # "X and reprograms" -> "X"
        words.pop()
        while words and words[-1].lower() in _STOP_TERMS:
            words.pop()
    t = " ".join(words)
    if len(t) < 2 or len(t) > 60:
        return None
    if t.lower() in _STOP_TERMS:
        return None
    return t


def relations(text):
    """-> [{"subject","object","kind","verb"}] with stop-words trimmed, de-duplicated."""
    out = []
    seen = set()
    for m in _REL_RE.finditer(text):
        subj, obj = _clean_term(m.group("subj")), _clean_term(m.group("obj"))
        if not subj or not obj or subj.lower() == obj.lower():
            continue
        verb = m.group("verb").lower()
        kind = _VERB2KIND.get(verb) or _VERB2KIND.get(verb.rstrip("s")) or "activation"
        key = (subj.lower(), obj.lower(), kind)
        if key in seen:
            continue
        seen.add(key)
        out.append({"subject": subj, "object": obj, "kind": kind, "verb": verb})
    for m in _ARROW_RE.finditer(text):
        subj, obj = _clean_term(m.group("subj")), _clean_term(m.group("obj"))
        if not subj or not obj or subj.lower() == obj.lower():
            continue
        key = (subj.lower(), obj.lower(), "activation")
        if key in seen:
            continue
        seen.add(key)
        out.append({"subject": subj, "object": obj, "kind": "activation", "verb": "\u2192"})
    return out


def classify(text):
    if all_numbers(text):
        return "number"
    if relations(text):
        return "relation"
    low = _norm(text).lower()
    if any(v in low for v in _METHOD):
        return "condition"
    if len(_norm(text)) < 90:
        return "entity"
    return "condition"


# --------------------------------------------------------------------------- #
# locator mapping
# --------------------------------------------------------------------------- #
def locator_for(mat, start, end):
    """Map a character span back onto the labels ingest.py produced."""
    locs = mat.get("locators") or []
    hits = [l for l in locs if l.get("start") is not None and l.get("end") is not None
            and l["start"] < end and l["end"] > start]
    if not hits:
        return None
    hits.sort(key=lambda l: (l["start"], l["end"]))
    first, last = hits[0].get("locator"), hits[-1].get("locator")
    if first == last:
        return first
    m1 = re.match(r"^([A-Za-z.]+)(\d+)$", str(first))
    m2 = re.match(r"^([A-Za-z.]+)(\d+)$", str(last))
    if m1 and m2 and m1.group(1) == m2.group(1):
        return "%s%s-%s" % (m1.group(1), m1.group(2), m2.group(2))
    return first


def _parse_locator(label):
    """-> ("line"|"page"|"row"|"ptr"|None, lo, hi) for range comparison."""
    s = str(label or "").strip()
    m = re.fullmatch(r"[Ll](\d+)(?:\s*-\s*[Ll]?(\d+))?", s)
    if m:
        return "line", int(m.group(1)), int(m.group(2) or m.group(1))
    m = re.fullmatch(r"[Pp]\.?\s*(\d+)(?:\s*-\s*[Pp]?\.?\s*(\d+))?", s)
    if m:
        return "page", int(m.group(1)), int(m.group(2) or m.group(1))
    m = re.fullmatch(r"[Rr]ow\s+(\d+)(?:\s*-\s*(\d+))?", s)
    if m:
        return "row", int(m.group(1)), int(m.group(2) or m.group(1))
    if s.startswith("$") or s.startswith("json:"):
        return "ptr", None, None
    return None, None, None


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def _claim_text(text):
    t = _norm(text)
    t = _HEADING.sub(r"\2", t)
    for prefix in ("Figure ", "Fig. ", "Table "):
        if t.startswith(prefix):
            pass
    return t


def _material_order(materials):
    mats = materials.get("materials") or []
    order = {"material": 0, "task": 1, "case": 2}
    return sorted(mats, key=lambda m: (order.get(m.get("role"), 3), m.get("path") or ""))


def _text_scheme_claims(materials):
    """Deterministic claim extraction from every textual material.

    Scientific claims come from the supplied *materials*.  The task text and
    case.json are only used when no material yielded text at all: quoting the
    task instructions would otherwise produce "problem -> method -> conclusion"
    as if it were a scientific pathway.
    """
    claims = []
    mats = [m for m in _material_order(materials)
            if m.get("parsed") and (m.get("text") or "").strip()]
    scientific = [m for m in mats if m.get("role") == "material"]
    chosen = scientific or mats
    for mat in chosen:
        kind = mat.get("kind") or ""
        if kind in ("csv", "tsv", "json"):
            continue
        for sent in sentences(mat["text"]):
            raw = sent["text"]
            plain = _norm(raw)
            if len(plain) < 25:
                continue
            if sent.get("heading"):
                # "Figure 03 legend" is document furniture, not a scientific claim;
                # turning it into a node label puts a caption number on the figure.
                if re.match(r"^(figure|table|scheme|box)\s*\d+", plain, re.I):
                    continue
                claims.append({"kind": "condition", "text": plain, "mat": mat,
                               "start": sent["start"], "end": sent["end"],
                               "section": sent.get("section") or plain, "value": None, "unit": None})
                continue
            kind_ = classify(raw)
            value, unit = last_number(raw) if kind_ == "number" else (None, None)
            entry = {"kind": kind_, "text": plain, "mat": mat,
                     "start": sent["start"], "end": sent["end"],
                     "section": sent.get("section") or "", "value": value, "unit": unit}
            if kind_ == "relation":
                entry["relations"] = relations(plain)
                if not entry["relations"]:
                    continue          # a "relation" claim with no parseable pair is noise
            if kind_ == "number":
                entry["numbers"] = [n for n in all_numbers(raw)][:8]
            claims.append(entry)
    return claims


def _table_claims(materials):
    """One checkable claim per table row (strided when a table is long)."""
    claims = []
    for mat in _material_order(materials):
        for table in (mat.get("tables") or []):
            rows = table.get("rows") or []
            if not rows:
                continue
            if len(rows) > MAX_TABLE_ROWS:
                step = max(1, len(rows) // MAX_TABLE_ROWS)
                picked = rows[::step][:MAX_TABLE_ROWS]
            else:
                picked = rows
            for row in picked:
                start, end = row.get("start"), row.get("end")
                if start is None or end is None or end <= start:
                    continue
                raw = (mat.get("text") or "")[start:end]
                if not _norm(raw):
                    continue
                cells = row.get("cells") or []
                value = None
                for cell in reversed(cells):
                    try:
                        value = float(str(cell).strip())
                        break
                    except (TypeError, ValueError):
                        continue
                claims.append({"kind": "number", "text": _norm(raw), "mat": mat,
                               "start": start, "end": end, "section": table.get("name") or "table",
                               "value": value, "unit": None, "row": row.get("n"),
                               "cells": cells, "header": table.get("header") or []})
    return claims


def build_ledger(materials):
    """-> {"items":[...], "counts":{...}, "warnings":[...]} (deterministic)."""
    claims = _text_scheme_claims(materials) + _table_claims(materials)
    items = []
    seen = set()
    for claim in claims:
        key = _norm(claim["text"]).lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(claim)
        if len(items) >= MAX_ITEMS:
            break

    ledger_items = []
    for i, claim in enumerate(items, start=1):
        mat = claim["mat"]
        loc = locator_for(mat, claim["start"], claim["end"])
        quote = _clip(claim["text"])
        item = {
            "id": "E%d" % i,
            "claim": claim["text"][:400],
            "kind": claim["kind"],
            "value": claim["value"],
            "unit": claim["unit"],
            "section": claim.get("section") or "",
            "source": {
                "file": mat.get("path"),
                "locator": loc or (mat.get("locator_scheme") or "line"),
                "quote": quote,
            },
        }
        if claim.get("cells"):
            item["cells"] = claim["cells"]
            item["header"] = claim.get("header")
            item["row"] = claim.get("row")
        if claim.get("relations"):
            item["relations"] = claim["relations"]
        if claim.get("numbers"):
            item["numbers"] = claim["numbers"]
        resolved = resolve_locator(item, materials)
        item["resolved"] = {"ok": bool(resolved.get("ok")), "file": resolved.get("file"),
                            "locator": resolved.get("locator"), "span": resolved.get("span"),
                            "reason": resolved.get("reason")}
        ledger_items.append(item)

    unresolved = [i["id"] for i in ledger_items if not (i.get("resolved") or {}).get("ok")]
    return {
        "items": ledger_items,
        "counts": {"items": len(ledger_items), "unresolved": len(unresolved),
                   "by_kind": _count_by(ledger_items, "kind")},
        "unresolved": unresolved,
        "warnings": list(materials.get("warnings") or []),
    }


def _count_by(items, key):
    out = {}
    for it in items:
        out[it.get(key)] = out.get(it.get(key), 0) + 1
    return out


def fallback_evidence(materials):
    """Last-resort, still-honest evidence: quote the input file itself."""
    for mat in _material_order(materials):
        text = mat.get("text") or ""
        if not text.strip():
            continue
        for sent in sentences(text):
            plain = _norm(sent["text"])
            if len(plain) >= 20:
                loc = locator_for(mat, sent["start"], sent["end"])
                item = {"id": "E1", "kind": "condition", "claim": plain[:400], "value": None, "unit": None,
                        "source": {"file": mat.get("path"), "locator": loc or "line",
                                   "quote": _clip(plain)}}
                item["resolved"] = resolve_locator(item, materials)
                return [item]
    # nothing readable at all: quote the case.json bytes that ingest already hashed
    for mat in _material_order(materials):
        if mat.get("role") == "case" and (mat.get("text") or "").strip():
            plain = _norm(mat["text"])[:200]
            item = {"id": "E1", "kind": "condition", "claim": "case input file", "value": None, "unit": None,
                    "source": {"file": mat.get("path"), "locator": "line", "quote": plain}}
            item["resolved"] = resolve_locator(item, materials)
            return [item]
    return []


# --------------------------------------------------------------------------- #
# resolution + provenance gate
# --------------------------------------------------------------------------- #
def _material_index(materials):
    mats = materials.get("materials") or []
    index = {}
    for mat in mats:
        path = mat.get("path") or ""
        index[path.replace("\\", "/")] = mat
        index[path.replace("\\", "/").lower()] = mat
        index[os.path.basename(path).lower()] = mat
        rel = path.replace("\\", "/")
        if rel.startswith("materials/"):
            index[rel[len("materials/"):].lower()] = mat
    return index


def _find_material(file_ref, materials):
    if not file_ref:
        return None
    index = _material_index(materials)
    key = str(file_ref).replace("\\", "/")
    for candidate in (key, key.lower(), os.path.basename(key).lower()):
        if candidate in index:
            return index[candidate]
    for mat in (materials.get("materials") or []):
        p = (mat.get("path") or "").replace("\\", "/")
        if p.endswith(key) or key.endswith(p):
            return mat
    return None


def resolve_locator(item, materials):
    """Prove that ``item['source']['quote']`` really occurs in the named material."""
    src = (item or {}).get("source") or {}
    file_ref = src.get("file")
    quote = src.get("quote")
    locator = src.get("locator")
    mat = _find_material(file_ref, materials)
    if mat is None:
        return {"ok": False, "file": file_ref, "locator": locator, "span": None,
                "reason": "material not found: %r" % (file_ref,)}
    text = mat.get("text") or ""
    if not text:
        return {"ok": False, "file": mat.get("path"), "locator": locator, "span": None,
                "reason": "material has no extracted text (kind=%s, %s)"
                          % (mat.get("kind"), mat.get("parse_reason"))}
    needle = _norm(quote)
    if not needle:
        return {"ok": False, "file": mat.get("path"), "locator": locator, "span": None,
                "reason": "empty quote"}
    # whitespace-normalised search, then map the hit back to raw offsets.
    # NOTE: `hay` is deliberately NOT stripped -- hay_map[i] must stay aligned
    # with hay[i], otherwise any material whose text begins with whitespace
    # (very common after PDF extraction or form-feed page joins) reports a span
    # shifted by that leading run and can lose the last character.
    hay_parts, hay_map = [], []
    for i, ch in enumerate(text):
        if ch.isspace():
            if hay_parts and hay_parts[-1] == " ":
                continue
            hay_parts.append(" ")
        else:
            hay_parts.append(ch)
        hay_map.append(i)
    hay = "".join(hay_parts)
    hits = []
    start = hay.find(needle)
    while start >= 0:
        raw_start = hay_map[start]
        raw_end = hay_map[min(start + len(needle) - 1, len(hay_map) - 1)] + 1
        hits.append((raw_start, raw_end))
        start = hay.find(needle, start + 1)
    if not hits:
        return {"ok": False, "file": mat.get("path"), "locator": locator, "span": None,
                "reason": "quote not found verbatim in %s" % mat.get("path")}

    # prefer the occurrence that actually sits inside the declared locator: a
    # quote that appears twice must not silently resolve to the wrong one
    raw_start, raw_end = hits[0]
    declared_span = _declared_span(locator, mat)
    if declared_span and len(hits) > 1:
        for cand_start, cand_end in hits:
            if cand_start >= declared_span[0] and cand_end <= declared_span[1]:
                raw_start, raw_end = cand_start, cand_end
                break
    found = locator_for(mat, raw_start, raw_end) or locator
    return {"ok": True, "file": mat.get("path"), "locator": found,
            "declared_locator": locator, "span": [raw_start, raw_end],
            "occurrences": len(hits),
            "locator_matches": _locator_agrees(locator, found),
            "reason": "verbatim match"}


def _declared_span(locator, mat):
    """Character range of a declared locator label, from the material's index."""
    label = str(locator or "").strip()
    if not label:
        return None
    scheme, lo, hi = _parse_locator(label)
    labels = {label, label.replace(" ", "")}
    if scheme == "line" and lo is not None:
        labels |= {"L%d" % n for n in range(lo, (hi or lo) + 1)}
    if scheme == "row" and lo is not None:
        labels |= {"row %d" % n for n in range(lo, (hi or lo) + 1)}
    if scheme == "page" and lo is not None:
        labels |= {"p.%d" % n for n in range(lo, (hi or lo) + 1)}
        labels |= {"p%d" % n for n in range(lo, (hi or lo) + 1)}
    if scheme == "ptr":
        # JSON pointers are emitted verbatim by ingest.py as "$/a/b"; match by
        # prefix so an exact "$/entities/1/label" resolves to its own span
        for l in (mat.get("locators") or []):
            if str(l.get("locator")) == label:
                return (l["start"], l["end"])
        return None
    spans = [l for l in (mat.get("locators") or []) if str(l.get("locator")) in labels]
    if not spans:
        return None
    return (min(s["start"] for s in spans), max(s["end"] for s in spans))


def _locator_agrees(declared, found):
    if not declared or not found:
        return None
    ds, dlo, dhi = _parse_locator(declared)
    fs, flo, fhi = _parse_locator(found)
    if ds is None or fs is None or ds != fs:
        return None
    return not (fhi < dlo or flo > dhi)


def _elements(spec):
    """Yield (json_path, element, evidence_ref) for everything that must be sourced.

    Text fields are yielded too: a number printed in a node's sublabel, a caption
    or an axis label is just as visible as one stored in ``value``, so it must be
    traceable by the same rule (otherwise "every printed number" is a claim the
    code does not keep).
    """
    for pi, panel in enumerate(spec.get("panels") or []):
        for ni, node in enumerate(panel.get("nodes") or []):
            yield ("$.panels[%d].nodes[%d]" % (pi, ni), node, node.get("sources") or node.get("source"))
        for ei, edge in enumerate(panel.get("edges") or []):
            yield ("$.panels[%d].edges[%d]" % (pi, ei), edge, edge.get("sources") or edge.get("source"))
        for ci, chart in enumerate(panel.get("charts") or []):
            for si, series in enumerate(chart.get("series") or []):
                yield ("$.panels[%d].charts[%d].series[%d]" % (pi, ci, si), series,
                       series.get("sources") or series.get("source"))
            # chart titles / axis labels / category labels are rendered text
            yield ("$.panels[%d].charts[%d]" % (pi, ci), _text_view(chart),
                   _chart_ref(chart))
        if isinstance(panel.get("legend"), dict):
            for it in (panel["legend"].get("items") or []):
                if isinstance(it, dict) and it.get("label"):
                    yield ("$.panels[%d].legend" % pi, _text_view(it), it.get("source"))
    texts = spec.get("texts") or {}
    if isinstance(texts, dict) and any(isinstance(v, str) and v for v in texts.values()):
        yield ("$.texts", _text_view(texts), texts.get("source"))
    meta = spec.get("meta") or {}
    if meta.get("title") or meta.get("subtitle"):
        yield ("$.meta", _text_view(meta), meta.get("source"))


def _chart_ref(chart):
    for s in chart.get("series") or []:
        if isinstance(s, dict):
            ref = s.get("sources") or s.get("source")
            if ref:
                return ref
    return chart.get("source")


def _text_view(obj):
    """A shallow copy exposing only the human-readable strings of an element."""
    out = {"_text_view": True}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                out[k] = v
            elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[k] = v
            elif isinstance(v, dict) and k == "x":
                for k2, v2 in v.items():
                    if isinstance(v2, str):
                        out["x." + k2] = v2
    return out


def _refs(ref):
    """Normalise an evidence reference to a list of ids."""
    if isinstance(ref, str):
        return [ref] if ref else []
    if isinstance(ref, (list, tuple)):
        return [r for r in ref if isinstance(r, str) and r]
    return []


def _norm_label(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def workorder_relations(materials):
    """Extract declared relations from any parsed JSON material that ships one.

    A case may hand the Skill an explicit structure file (entity list + relation
    list). That file is a *workorder*: it states which arrows the figure is
    expected to draw. Nothing used to read it, so the paper's headline arrow could
    be omitted - or drawn backwards - and every gate still passed.
    """
    out = []
    for mat in (materials.get("materials") or []):
        if not mat.get("parsed") or (mat.get("kind") or "") != "json":
            continue
        try:
            data = json.loads(mat.get("text") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        labels = {}
        for ent in (data.get("entities") or []):
            if isinstance(ent, dict) and isinstance(ent.get("id"), str):
                labels[ent["id"]] = ent.get("label") or ent["id"]
        for rel in (data.get("relations") or []):
            if not isinstance(rel, dict):
                continue
            a, b = rel.get("from"), rel.get("to")
            if not isinstance(a, str) or not isinstance(b, str):
                continue
            out.append({"subject": labels.get(a, a), "object": labels.get(b, b),
                        "kind": rel.get("kind") or "activation",
                        "file": mat.get("path"), "raw": (a, b)})
    return out


def check_workorder(spec, materials):
    """E908: a relation the supplied structure file declares that the figure gets wrong."""
    declared = workorder_relations(materials)
    if not declared:
        return []
    issues = []
    # a label can legitimately appear in more than one panel, so map a label to
    # every node that carries it and accept the relation if any pair is wired
    label_ids = {}
    for panel in (spec.get("panels") or []):
        for node in (panel.get("nodes") or []):
            label_ids.setdefault(_norm_label(node.get("label")), []).append(node.get("id"))
    edges = set()
    for panel in (spec.get("panels") or []):
        for edge in (panel.get("edges") or []):
            edges.add((edge.get("from"), edge.get("to")))

    def resolve(name):
        key = _norm_label(name)
        if key in label_ids:
            return list(label_ids[key])
        for lab, ids in label_ids.items():
            if lab and (lab in key or key in lab):
                return list(ids)
        return []

    missing, reversed_ = [], []
    for rel in declared:
        a_ids, b_ids = resolve(rel["subject"]), resolve(rel["object"])
        if not a_ids or not b_ids:
            continue                       # the entity is genuinely not on the figure
        if any((a, b) in edges for a in a_ids for b in b_ids):
            continue
        if any((b, a) in edges for a in a_ids for b in b_ids):
            reversed_.append("%s->%s" % (rel["subject"], rel["object"]))
        else:
            missing.append("%s->%s" % (rel["subject"], rel["object"]))
    for pair in reversed_:
        issues.append({"code": "E908", "severity": "error", "path": "$.panels",
                       "message": "declared relation %s is drawn in the REVERSE direction" % pair})
    if missing:
        issues.append({"code": "E909", "severity": "warn", "path": "$.panels",
                       "message": "%d relation(s) declared by the supplied structure file are not "
                                  "drawn: %s" % (len(missing), ", ".join(missing[:4]))})
    return issues


def check_provenance(spec, materials):
    """Gate E. Returns issues in scene.validate_spec's {code,severity,path,message} shape."""
    issues = []
    ev = {e.get("id"): e for e in (spec.get("evidence") or []) if isinstance(e, dict)}
    usable = [m for m in (materials.get("materials") or [])
              if m.get("parsed") and (m.get("text") or "").strip()]

    # The structural walk runs FIRST: an element with no source at all (E903) is a
    # hard error even when no material text is available to resolve quotes. An
    # earlier version returned early on E900, so a completely unsourced figure
    # degraded to a warning instead of failing.
    refs_seen = []
    for jpath, element, ref in _elements(spec):
        refs = _refs(ref)
        is_text_view = bool(isinstance(element, dict) and element.get("_text_view"))
        if not refs:
            if is_text_view:
                continue                      # the owning element reports its own E903
            issues.append({"code": "E903", "severity": "error", "path": jpath,
                           "message": "element has no evidence source"})
            continue
        refs_seen.extend(refs)
        missing = [r for r in refs if r not in ev]
        if missing:
            issues.append({"code": "E904", "severity": "error", "path": jpath,
                           "message": "element cites evidence id(s) %s which are not in spec.evidence"
                                      % (", ".join(missing),)})
            continue
        broken = []
        for r in refs:
            if (ev[r].get("resolved") or {}).get("ok") is False:
                res = resolve_locator(ev[r], materials)
                if not res.get("ok"):
                    broken.append("%s (%s)" % (r, res.get("reason")))
        if broken:
            issues.append({"code": "E905", "severity": "error", "path": jpath,
                           "message": "element cites evidence whose quote does not resolve: %s"
                                      % "; ".join(broken)})
        # a number printed on the figure must occur in the evidence it cites --
        # otherwise a real evidence id can launder a fabricated value
        quotes = [(ev[r].get("source") or {}).get("quote") or "" for r in refs]
        if isinstance(element, dict) and element.get("derived") and not element.get("source_values"):
            issues.append({"code": "E907", "severity": "warn", "path": jpath,
                           "message": "element is marked derived but declares no source_values, so "
                                      "the displayed number cannot be recomputed from its source"})
        for value in _element_values(element):
            if not any(_number_in_text(value, q) for q in quotes):
                issues.append({"code": "E906", "severity": "error", "path": jpath,
                               "message": "value %r is not present in the quote of evidence %s "
                                          "(figure would show a number its source does not contain)"
                                          % (value, ", ".join(refs))})

    if not usable:
        issues.append({"code": "E900", "severity": "warn", "path": "$.evidence",
                       "message": "UNVERIFIABLE: no parsed material text is available, so no quote "
                                  "can be resolved (this is a data limitation, not a pass)"})
    issues.extend(check_workorder(spec, materials))

    for eid, item in sorted(ev.items(), key=lambda kv: str(kv[0])):
        if eid not in refs_seen:
            continue                          # unreferenced ledger entries are not claims on the page
        res = resolve_locator(item, materials)
        path = "$.evidence[%s]" % eid
        if not res.get("ok") and usable:
            issues.append({"code": "E901", "severity": "error", "path": path,
                           "message": "evidence %s does not resolve: %s" % (eid, res.get("reason"))})
        elif res.get("locator_matches") is False:
            issues.append({"code": "E902", "severity": "warn", "path": path,
                           "message": "evidence %s resolves but the declared locator %r disagrees "
                                      "with the found locator %r"
                                      % (eid, res.get("declared_locator"), res.get("locator"))})
    return issues


def _element_values(element):
    """Every number the element can print, from ``value`` *and* from its text.

    Text matters: "1% O2 (hypoxia)" or "ATP 18.7 -> 12.4 nmol/mg" live in a
    sublabel, are perfectly visible in the figure, and used to escape the
    number-in-quote check entirely.
    """
    out = []
    if not isinstance(element, dict):
        return out

    def add(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(v)

    if element.get("derived"):
        for v in element.get("source_values") or []:
            add(v)
    else:
        add(element.get("value"))
        for v in element.get("values") or []:
            add(v)
    for key, val in element.items():
        if key in ("value", "values", "source_values", "source", "sources", "id", "from", "to"):
            continue
        if isinstance(val, str):
            out.extend(all_numbers(val))
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    out.extend(all_numbers(item))
    # de-duplicate, keep order
    seen, uniq = set(), []
    for v in out:
        key = round(float(v), 6)
        if key not in seen:
            seen.add(key)
            uniq.append(v)
    return uniq


def _number_in_text(value, text):
    """True if ``value`` appears in ``text`` under any of the usual renderings."""
    if value is None:
        return True
    candidates = []
    try:
        candidates.append("%g" % value)
        candidates.append("%.1f" % value)
        candidates.append("%.2f" % value)
        candidates.append("%.3f" % value)
        candidates.append(str(int(value)) if float(value).is_integer() else str(value))
        if float(value).is_integer():
            candidates.append("%02d" % int(value))       # "Figure 03" contains 3
    except (TypeError, ValueError):
        return True
    hay = _norm(text)
    for cand in candidates:
        if re.search(r"(?<![0-9.])" + re.escape(cand) + r"(?![0-9])", hay):
            return True
    return False

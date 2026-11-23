#!/usr/bin/env python3
"""measure.py — deterministic text measurement, wrapping and fitting.

Contract §4 (frozen signatures):
    text_width_mm(text, size_pt, fs, bold=False) -> float
    wrap_text(text, size_pt, fs, max_w_mm)       -> list[str]
    line_height_mm(size_pt, leading=1.25)        -> float

Measurement order (never raises, degrades silently):
    1. real font tables read with fontTools (hmtx + head.unitsPerEm + cmap)
       — exact advance widths, kerning ignored;
    2. matplotlib's ft2font, but only when it is already imported or explicitly
       requested (``GA_USE_FT2FONT=1``).  On this host ``import matplotlib``
       costs ~20-50 s because of its package-level imports, so paying it on the
       default path would blow the run budget for zero accuracy gain: both
       paths read the same OpenType tables and agree to the last bit;
    3. a built-in Helvetica AFM width table (last resort, ASCII only).

The ft2font path is kept because it is the contract's preferred reader and is
used automatically whenever something else in the process already pulled it in.

All widths are computed at a 12 pt reference and scaled linearly, so the result
is independent of accumulation order and identical on every run.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import math
import os
import unicodedata

import scene

_REF_PT = 12.0

_TABLES = {}          # abspath -> dict|None  (fontTools tables)
_FT_CACHE = {}        # abspath -> ft2font.FT2Font|None
_EM_CACHE = {}        # (abspath, char) -> float em | None
_IMPORT_FAILED = False

# Helvetica/Arial AFM advance widths, per 1000 em (public metrics).
_AFM = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
    "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
    ".": 278, "/": 278, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584,
    "?": 556, "@": 1015, "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556,
    "`": 333, "{": 334, "|": 260, "}": 334, "~": 584,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
    "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
    "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556,
    "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556,
    "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
}
for _d in "0123456789":
    _AFM[_d] = 556

_DEFAULT_EM = 0.556


# --------------------------------------------------------------------------- #
# face loading
# --------------------------------------------------------------------------- #
def _use_ft2font():
    if os.environ.get("GA_USE_FT2FONT") == "1":
        return True
    return "matplotlib.ft2font" in __import__("sys").modules


def font_tables(path):
    """{"upem","cmap"(code->glyphname),"hmtx"(glyphname->(adv,lsb))} or None."""
    if not path:
        return None
    key = os.path.abspath(str(path))
    if key in _TABLES:
        return _TABLES[key]
    tab = None
    if os.path.exists(key):
        try:
            from fontTools.ttLib import TTFont  # noqa: WPS433 (optional dependency)
            font = TTFont(key, fontNumber=0, lazy=True)
            head = font["head"]
            cmap = font.getBestCmap() or {}
            hmtx = {}
            for gname, metric in font["hmtx"].metrics.items():
                hmtx[gname] = (metric[0], metric[1])
            tab = {"upem": float(head.unitsPerEm) or 1000.0, "cmap": cmap, "hmtx": hmtx}
            try:
                font.close()
            except Exception:
                pass
        except Exception:
            tab = None
    _TABLES[key] = tab
    return tab


def _ft_face(path):
    if not path or not _use_ft2font():
        return None
    key = os.path.abspath(str(path))
    if key in _FT_CACHE:
        return _FT_CACHE[key]
    face = None
    try:
        from matplotlib import ft2font  # noqa: WPS433
        face = ft2font.FT2Font(key)
        face.set_size(_REF_PT, 72.0)
    except Exception:
        face = None
    _FT_CACHE[key] = face
    return face


def _char_em(ch, path):
    """Advance width of one character in em units, or None when unknown."""
    key = (os.path.abspath(str(path)) if path else "", ch)
    if key in _EM_CACHE:
        return _EM_CACHE[key]
    em = None
    code = ord(ch)
    face = _ft_face(path)
    if face is not None:
        try:
            if face.get_char_index(code) or code == 0:
                em = float(face.load_char(code).linearHoriAdvance) / 65536.0 / _REF_PT
        except Exception:
            em = None
    if em is None:
        tab = font_tables(path)
        if tab is not None:
            gname = tab["cmap"].get(code)
            if gname is not None:
                metric = tab["hmtx"].get(gname)
                if metric is not None:
                    em = metric[0] / tab["upem"]
        elif path and ch in _AFM:
            em = _AFM[ch] / 1000.0
    _EM_CACHE[key] = em
    return em


def char_supported(path, code):
    """True/False when the face can be read, None when it cannot."""
    tab = font_tables(path)
    if tab is not None:
        return code in tab["cmap"]
    face = _ft_face(path)
    if face is not None:
        try:
            return bool(face.get_char_index(code)) or code == 0
        except Exception:
            return None
    return None


def is_cjk(ch):
    """True for characters that must be measured with a CJK-capable font."""
    code = ord(ch)
    return (
        0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE4F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x20000 <= code <= 0x3FFFF
    )


def _fallback_em(text):
    total = 0.0
    for ch in text:
        if ch == "\t":
            total += 2.0
        elif is_cjk(ch):
            total += 1.0
        elif unicodedata.combining(ch):
            total += 0.0
        else:
            total += _AFM.get(ch, _DEFAULT_EM) / 1000.0
    return total


def _pick_path(text, fs, bold):
    """Font path that will actually be used to render `text`."""
    if fs is None:
        return None
    chooser = getattr(fs, "font_for", None)
    if callable(chooser):
        try:
            p = chooser(text, bold=bold)
            if p:
                return p
        except Exception:
            pass
    if bold and getattr(fs, "bold", None):
        return fs.bold
    return getattr(fs, "regular", None)


# --------------------------------------------------------------------------- #
# public API (contract §4)
# --------------------------------------------------------------------------- #
def text_width_mm(text, size_pt, fs, bold=False):
    """Width of `text` set in `size_pt`, in millimetres. Never raises."""
    if not text:
        return 0.0
    size = float(size_pt)
    path = _pick_path(text, fs, bold)
    em_total = 0.0
    exact = bool(path)
    if path:
        for ch in text:
            em = _char_em(ch, path)
            if em is None:
                exact = False
                break
            em_total += em
    if not exact:
        em_total = _fallback_em(text)
    return em_total * size * scene.PT_MM


def line_height_mm(size_pt, leading=1.25):
    """Baseline-to-baseline distance in mm."""
    return float(size_pt) * float(leading) * scene.PT_MM


def _tokens(text):
    """Split into wrap units: CJK chars stand alone, latin runs stay whole."""
    out, buf = [], []
    for ch in text:
        if is_cjk(ch) or ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(ch)
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _hard_split(token, size_pt, fs, max_w_mm, bold):
    """Break a single over-wide token character by character."""
    parts, cur = [], ""
    for ch in token:
        trial = cur + ch
        if cur and text_width_mm(trial, size_pt, fs, bold) > max_w_mm:
            parts.append(cur)
            cur = ch
        else:
            cur = trial
    if cur:
        parts.append(cur)
    return parts or [token]


def wrap_text(text, size_pt, fs, max_w_mm, bold=False):
    """Greedy wrap to `max_w_mm`; honours explicit newlines; never returns [].

    `bold` must be passed for bold text: advance widths differ between the
    regular and bold faces, and wrapping a bold label with regular metrics is
    what lets a title overrun the box it was measured for.
    """
    size_pt = float(size_pt)
    max_w = max(0.5, float(max_w_mm))
    lines = []
    for para in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        para = para.strip()
        if not para:
            lines.append("")
            continue
        cur = ""
        for tok in _tokens(para):
            if tok.isspace():
                if cur:
                    cur += " "
                continue
            trial = (cur + tok) if cur else tok
            if text_width_mm(trial.rstrip(), size_pt, fs, bold) <= max_w:
                cur = trial
                continue
            if cur:
                lines.append(cur.rstrip())
                cur = ""
            if text_width_mm(tok, size_pt, fs, bold) <= max_w:
                cur = tok
            else:
                parts = _hard_split(tok, size_pt, fs, max_w, bold)
                lines.extend(parts[:-1])
                cur = parts[-1]
        lines.append(cur.rstrip())
    while len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines or [""]


def fit_text(text, size_pt, fs, max_w_mm, max_h_mm, min_pt=None,
             leading=1.25, bold=False, shrink_step=0.5):
    """Wrap, then shrink towards `min_pt` until the block fits its box.

    The height budget is interpreted as *how many lines fit* (a 2.6 mm box at
    7 pt holds one line, not zero), so a single short label in a tight strip is
    not reported as an overflow.

    Returns {"lines","size_pt","line_count","width_mm","height_mm","shrunk",
             "floor_hit","overflow","max_lines","dropped_lines"}.

    `floor_hit` is True when the block still does not fit at `min_pt`: the
    caller must emit a scene warning (contract §6 B) — text is never silently
    allowed to leave its box.  `dropped_lines` counts the wrapped lines the
    caller must not draw.
    """
    if min_pt is None:
        min_pt = scene.MIN_BODY_PT
    size = float(size_pt)
    hard_floor = max(1.0, float(min_pt))
    max_w = max(0.5, float(max_w_mm))
    max_h = max(0.5, float(max_h_mm))
    shrunk = False

    def _measure(size_):
        lines_ = wrap_text(text, size_, fs, max_w, bold)
        lh_ = line_height_mm(size_, leading)
        allowed_ = max(1, int(math.floor(max_h / lh_ + 1e-9)))
        w_ = max([text_width_mm(ln, size_, fs, bold) for ln in lines_] or [0.0])
        return lines_, lh_, allowed_, w_

    lines, lh, allowed, w = _measure(size)
    while (w > max_w + 1e-9 or len(lines) > allowed) and size > hard_floor:
        nxt = round(size - shrink_step, 3)
        if nxt < hard_floor:
            nxt = hard_floor
        if nxt == size:
            break
        size = nxt
        shrunk = True
        lines, lh, allowed, w = _measure(size)
    h = lh * len(lines)
    return {
        "lines": lines,
        "size_pt": size,
        "line_count": len(lines),
        "width_mm": w,
        "height_mm": h,
        "shrunk": shrunk,
        "floor_hit": bool(w > max_w + 1e-9 or len(lines) > allowed),
        "overflow": bool(w > max_w + 1e-9),
        "max_lines": allowed,
        "dropped_lines": max(0, len(lines) - allowed),
    }


def clear_caches():
    """Drop measurement caches (tests only; not needed for determinism)."""
    _TABLES.clear()
    _FT_CACHE.clear()
    _EM_CACHE.clear()

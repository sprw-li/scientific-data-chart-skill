#!/usr/bin/env python3
"""fonts.py — font discovery, CJK coverage and glyph-missing detection.

Contract §4 (frozen signatures):
    class FontSet: regular, bold, cjk, family_css, paths, degraded
    resolve_fonts(lang="auto") -> FontSet
    missing_glyphs(text, font_path) -> list[str]
    glyph_coverage_ok(text, fs) -> bool

Design rules:
    * never raise on a missing font — degrade and record it;
    * resolution is a pure function of the filesystem, so two runs of the same
      machine pick the same face (no randomness, no time, no environment scan
      order: every candidate list is explicit and ordered);
    * FontSet carries `family_css`, a real fallback chain for the SVG.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import os

# --------------------------------------------------------------------------- #
# candidate tables (explicit + ordered => deterministic)
# --------------------------------------------------------------------------- #
# family name -> (regular filename, bold filename, face index)
_LATIN_CANDIDATES = [
    ("arial.ttf", "arialbd.ttf"),
    ("calibri.ttf", "calibrib.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("liberationsans.ttf", "liberationsansbold.ttf"),
    ("freesans.ttf", "freesansbold.ttf"),
]

_CJK_CANDIDATES = [
    # plain .ttf first: TTC collections are awkward for PIL/matplotlib PDF
    ("simhei.ttf", "simhei.ttf", 0),
    ("msyh.ttc", "msyhbd.ttc", 0),
    ("msyh.ttf", "msyhbd.ttf", 0),
    ("NotoSansSC-VF.ttf", "NotoSansSC-VF.ttf", 0),
    ("NotoSansCJKsc-Regular.otf", "NotoSansCJKsc-Bold.otf", 0),
    ("simsun.ttc", "simsun.ttc", 0),
    ("NotoSansJP-VF.ttf", "NotoSansJP-VF.ttf", 0),
    ("NotoSansKR-VF.ttf", "NotoSansKR-VF.ttf", 0),
]

# Visual fallback chains (names only — they must be present for the SVG to be
# portable; the renderer picks whatever row above actually resolved).
_LATIN_FALLBACKS = ["Helvetica Neue", "Helvetica", "Arial", "Segoe UI", "DejaVu Sans"]
_CJK_FALLBACKS = ["Microsoft YaHei", "SimHei", "Noto Sans SC", "Source Han Sans SC",
                  "PingFang SC", "Hiragino Sans GB", "WenQuanYi Zen Hei"]

_FAMILY_BY_FILE = {
    "arial.ttf": "Arial", "arialbd.ttf": "Arial", "ariali.ttf": "Arial",
    "calibri.ttf": "Calibri", "calibrib.ttf": "Calibri",
    "segoeui.ttf": "Segoe UI", "segoeuib.ttf": "Segoe UI",
    "times.ttf": "Times New Roman", "timesbd.ttf": "Times New Roman",
    "simhei.ttf": "SimHei", "simsun.ttc": "SimSun", "simsunb.ttf": "SimSun",
    "msyh.ttc": "Microsoft YaHei", "msyhbd.ttc": "Microsoft YaHei",
    "msyhl.ttc": "Microsoft YaHei Light",
    "NotoSansSC-VF.ttf": "Noto Sans SC", "NotoSerifSC-VF.ttf": "Noto Serif SC",
    "NotoSansJP-VF.ttf": "Noto Sans JP", "NotoSansKR-VF.ttf": "Noto Sans KR",
    "DejaVuSans.ttf": "DejaVu Sans", "DejaVuSans-Bold.ttf": "DejaVu Sans",
}

CJK_LANGS = ("zh", "cn", "chinese", "ja", "jp", "japanese", "ko", "kr", "korean")


def _cjk_requested(lang):
    lang = (lang or "auto").strip().lower()
    if lang in ("auto", ""):
        return True          # unknown language: must stay CJK-capable
    return any(lang.startswith(p) for p in CJK_LANGS) or "han" in lang


def _font_dirs():
    dirs = []
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    dirs.append(os.path.join(windir, "Fonts"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    home = os.environ.get("HOME") or os.path.expanduser("~")
    dirs.append(os.path.join(home, ".fonts"))
    dirs.append("/usr/share/fonts")
    dirs.append("/Library/Fonts")
    # matplotlib ships DejaVu (last-resort face). Resolve its path without
    # importing matplotlib: on this host `import matplotlib` costs ~20-50 s
    # because of package-level imports, which must not sit on the build path.
    import sys as _sys
    import sysconfig
    sp_dirs = []
    try:
        sp_dirs.append(sysconfig.get_paths().get("purelib"))
        sp_dirs.append(sysconfig.get_paths().get("platlib"))
    except Exception:
        pass
    sp_dirs.extend([p for p in _sys.path if p and p.endswith("site-packages")])
    for sp in sp_dirs:
        if sp:
            dirs.append(os.path.join(sp, "matplotlib", "mpl-data", "fonts", "ttf"))
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _find(filename):
    if not filename:
        return None
    for d in _font_dirs():
        p = os.path.join(d, filename)
        if os.path.isfile(p):
            return p
    return None


def _family_of(path, fallback_name=None):
    """Real family name from the font file, else a filename-derived guess."""
    if path:
        try:
            from fontTools.ttLib import TTFont  # noqa: WPS433
            font = TTFont(path, fontNumber=0, lazy=True)
            name_table = font["name"]
            best = ""
            for name_id in (16, 1):          # typographic family, then family
                for rec in name_table.names:
                    if rec.nameID != name_id:
                        continue
                    try:
                        val = (rec.toUnicode() or "").strip()
                    except Exception:
                        continue
                    if val:
                        best = val
                        break
                if best:
                    break
            try:
                font.close()
            except Exception:
                pass
            if best:
                return best
        except Exception:
            pass
    base = os.path.basename(path or "").lower()
    if base in _FAMILY_BY_FILE:
        return _FAMILY_BY_FILE[base]
    if fallback_name:
        return fallback_name
    return "sans-serif"


def _css_name(family):
    family = (family or "sans-serif").strip()
    if family.lower() in ("sans-serif", "serif", "monospace", "cursive", "fantasy"):
        return family.lower()
    return "'%s'" % family.replace("'", "")


# --------------------------------------------------------------------------- #
# FontSet
# --------------------------------------------------------------------------- #
class FontSet(object):
    """Resolved faces for one figure. Paths are absolute; `None` means missing."""

    def __init__(self, regular=None, bold=None, cjk=None, family_css="sans-serif",
                 paths=None, degraded=False, lang="auto", indices=None,
                 families=None, notes=None):
        self.regular = regular
        self.bold = bold or regular
        self.cjk = cjk
        self.family_css = family_css
        self.paths = paths or {"regular": regular, "bold": self.bold, "cjk": cjk}
        self.indices = indices or {"regular": 0, "bold": 0, "cjk": 0}
        self.families = families or {}
        self.degraded = bool(degraded)
        self.lang = lang or "auto"
        self.notes = list(notes or [])
        self.missing_sample = []

    # -- selection ---------------------------------------------------------- #
    def face_index(self, path):
        try:
            return int(self.indices.get(self._role_of(path), 0))
        except Exception:
            return 0

    def _role_of(self, path):
        if path and path == self.cjk:
            return "cjk"
        if path and path == self.bold:
            return "bold"
        return "regular"

    def font_for(self, text, bold=False):
        """Path used to render `text` (CJK face wins when a CJK char appears)."""
        from measure import is_cjk  # local import: measure.py has no module-level dep on us
        if text and any(is_cjk(ch) for ch in text) and self.cjk:
            return self.cjk
        if bold and self.bold:
            return self.bold
        return self.regular

    def index_for(self, path):
        for role, p in self.paths.items():
            if p and path and os.path.abspath(p) == os.path.abspath(path):
                return int(self.indices.get(role, 0))
        return 0

    def all_paths(self):
        out = []
        for role in ("regular", "bold", "cjk"):
            p = self.paths.get(role)
            if p and p not in out:
                out.append(p)
        return out

    def record_missing(self, chars):
        if not chars:
            return
        for ch in chars:
            if ch not in self.missing_sample:
                self.missing_sample.append(ch)

    def describe(self):
        return {
            "lang": self.lang,
            "family_css": self.family_css,
            "regular": self.regular,
            "bold": self.bold,
            "cjk": self.cjk,
            "families": dict(self.families),
            "degraded": self.degraded,
            "missing_sample": "".join(self.missing_sample),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def resolve_fonts(lang="auto"):
    """Best available family for `lang`. Never raises, never returns None."""
    notes = []
    indices = {}
    families = {}

    regular = bold = None
    for cand, cand_bold in _LATIN_CANDIDATES:
        p = _find(cand)
        if p:
            regular = p
            bold = _find(cand_bold) or p
            indices["regular"] = 0
            indices["bold"] = 0
            families["regular"] = _family_of(p, "Arial")
            families["bold"] = _family_of(bold, families["regular"])
            break

    cjk = None
    cjk_index = 0
    for cand, _cand_bold, idx in _CJK_CANDIDATES:
        p = _find(cand)
        if p:
            cjk = p
            cjk_index = idx
            families["cjk"] = _family_of(p, "SimHei")
            break
    if cjk:
        indices["cjk"] = cjk_index

    if cjk and cjk in (regular, bold):
        cjk = None
        families.pop("cjk", None)

    need_cjk = _cjk_requested(lang)
    degraded = bool(need_cjk and not cjk)
    if not regular:
        notes.append("no latin font file found; falling back to the built-in width table")
    if not cjk:
        notes.append("no CJK font file found; CJK text will be flagged as degraded")
    if need_cjk and cjk:
        notes.append("CJK-capable face resolved: %s" % os.path.basename(cjk))

    chain = []
    for fam in [families.get("regular")] + _LATIN_FALLBACKS + _CJK_FALLBACKS:
        if fam and fam not in chain:
            chain.append(fam)
    if cjk and families.get("cjk") and families["cjk"] not in chain:
        chain.append(families["cjk"])
    chain.append("sans-serif")
    family_css = ", ".join(_css_name(f) for f in chain)

    fs = FontSet(regular=regular, bold=bold, cjk=cjk, family_css=family_css,
                 paths={"regular": regular, "bold": bold, "cjk": cjk},
                 degraded=degraded, lang=lang or "auto", indices=indices,
                 families=families, notes=notes)
    return fs


# --------------------------------------------------------------------------- #
# glyph coverage
# --------------------------------------------------------------------------- #
_CMAP_CACHE = {}


def _charmap(path):
    """Set of codepoints the face can draw, or None when unreadable. Cached."""
    if not path:
        return None
    key = os.path.abspath(str(path))
    if key in _CMAP_CACHE:
        return _CMAP_CACHE[key]
    cmap = None
    if os.path.exists(key):
        from measure import font_tables, char_supported  # noqa: WPS433
        tab = font_tables(key)
        if tab is not None:
            cmap = set(tab["cmap"].keys())
        elif char_supported(key, 0) is None:
            cmap = None
    _CMAP_CACHE[key] = cmap
    return cmap


def missing_glyphs(text, font_path):
    """Sorted list of characters in `text` that `font_path` cannot draw.

    When the face cannot be read at all, only characters outside the built-in
    ASCII width table are reported (they are the ones with no safe fallback).
    """
    if not text:
        return []
    cmap = _charmap(font_path) if font_path else None
    if cmap is None:
        from measure import _AFM  # noqa: WPS433
        out = []
        for ch in text:
            if ch.isspace() or ch in _AFM:
                continue
            if ch not in out:
                out.append(ch)
        return sorted(out)
    out = []
    for ch in text:
        if ch.isspace():
            continue
        if ord(ch) not in cmap and ch not in out:
            out.append(ch)
    return sorted(out)


def glyph_coverage_ok(text, fs):
    """True when every non-space character is drawable by one of fs' faces."""
    if not text:
        return True
    if fs is None:
        return False
    remaining = set(text) - set(" \t\n\r")
    if not remaining:
        return True
    for p in fs.all_paths():
        cmap = _charmap(p)
        if cmap is None:
            continue
        remaining = {ch for ch in remaining if ord(ch) not in cmap}
        if not remaining:
            return True
    if remaining:
        fs.record_missing(sorted(remaining))
    return not remaining


def coverage_report(text, fs):
    """Detail dict for reports: which chars are missing from every resolved face."""
    missing = []
    for ch in sorted(set(text or "")):
        if ch.isspace():
            continue
        if not glyph_coverage_ok(ch, fs):
            missing.append(ch)
    return {
        "ok": not missing,
        "missing": "".join(missing),
        "missing_count": len(missing),
        "faces": [os.path.basename(p) for p in (fs.all_paths() if fs else [])],
    }

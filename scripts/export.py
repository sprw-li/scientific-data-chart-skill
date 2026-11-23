#!/usr/bin/env python3
"""export.py — three-format export orchestration + export report (contract §3/§4).

Owner: validate-export.  Writes nothing but the artefacts under ``out_dir`` and the
returned report dict; ``chart.py`` is the only writer of ``export_report.json``.

Public API (frozen, contract §4):
    export_all(scene, spec, fs, out_dir, formats=("svg","png","pdf")) -> dict

Returned dict keys (chart.py + result.json read these):
    artifacts      {name: {path, bytes, sha256}}
    page_mm        [w, h]                 (mm)
    dpi            int
    pdf            {pages, fonts_embedded, vector, ...}
    png            {px: [w, h], dpi, ...}
    determinism    {identical, hashes, identical_by_format}
    colorspace     "sRGB"

Also exposes the byte-level sniffers used by validate.py gate H so that the
measurement lives in exactly one place:
    sniff_png(path) -> dict
    sniff_pdf(path) -> dict

The backend modules (svg_backend / raster_backend, owned by render-core) are
imported lazily so this module stays importable (and testable with fixtures)
when the rendering half is not installed yet.  A missing backend is an explicit
error, never a silent skip.
"""
from __future__ import annotations
import sys as _sys
_sys.dont_write_bytecode = True

import hashlib
import importlib
import os
import re
import shutil
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import scene  # noqa: E402  (frozen shared model)

MM_PER_IN = 25.4
FORMAT_EXT = {"svg": "figure.svg", "png": "figure.png", "pdf": "figure.pdf",
              "pptx": "figure.pptx", "eps": "figure.eps", "tiff": "figure.tiff"}
CORE_FORMATS = ("svg", "png", "pdf")
OPTIONAL_FORMATS = ("pptx", "eps", "tiff")
ARTIFACT_ORDER = ("figure.svg", "figure.png", "figure.pdf")
MM_EMU = 36000  # 1 mm = 36000 EMU (English Metric Units, used by OOXML)


class ExportError(RuntimeError):
    """A backend is missing or failed; the caller must surface this, not ignore it."""


# --------------------------------------------------------------------------- #
# deterministic small helpers
# --------------------------------------------------------------------------- #
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _import_backend(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ExportError("backend module %r is not available: %s" % (name, exc))


def resolve_canvas(scene_obj, spec):
    """Page geometry: scene.canvas wins, then spec.meta.canvas, then defaults."""
    canvas = dict(scene.DEFAULT_CANVAS)
    meta_canvas = ((spec or {}).get("meta") or {}).get("canvas") or {}
    for k, v in meta_canvas.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            canvas[k] = float(v)
    sc = (scene_obj or {}).get("canvas") or {}
    for k, v in sc.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            canvas[k] = float(v)
    return canvas


# --------------------------------------------------------------------------- #
# byte-level sniffers (single source of truth for gate H)
# --------------------------------------------------------------------------- #
def _inflate_pdf_streams(raw):
    """Return the concatenation of every zlib-decodable PDF stream body."""
    parts = []
    idx = 0
    while True:
        s = raw.find(b"stream", idx)
        if s < 0:
            break
        e = raw.find(b"endstream", s)
        if e < 0:
            break
        body = raw[s + 6:e]
        if body.startswith(b"\r\n"):
            body = body[2:]
        elif body[:1] in (b"\n", b"\r"):
            body = body[1:]
        try:
            parts.append(zlib.decompress(body))
        except Exception:
            try:
                parts.append(zlib.decompressobj().decompress(body))
            except Exception:
                pass
        idx = e + 9
    return b"".join(parts)


def sniff_png(path):
    """Parse PNG chunks without third-party imports.

    Returns {"px":[w,h], "dpi":float|None, "dpi_x":..., "dpi_y":...,
             "has_phys":bool, "bit_depth":int|None, "color_type":int|None,
             "chunks":[names], "bytes":n}
    """
    out = {"px": None, "dpi": None, "dpi_x": None, "dpi_y": None, "has_phys": False,
           "bit_depth": None, "color_type": None, "chunks": [], "bytes": _size(path)}
    if not os.path.exists(path):
        out["error"] = "missing"
        return out
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        out["error"] = "not a PNG signature"
        return out
    pos = 8
    while pos + 12 <= len(raw):
        ln = int.from_bytes(raw[pos:pos + 4], "big")
        typ = raw[pos + 4:pos + 8].decode("latin-1")
        data = raw[pos + 8:pos + 8 + ln]
        out["chunks"].append(typ)
        if typ == "IHDR" and ln >= 13:
            out["px"] = [int.from_bytes(data[0:4], "big"), int.from_bytes(data[4:8], "big")]
            out["bit_depth"] = data[8]
            out["color_type"] = data[9]
        elif typ == "pHYs" and ln >= 9:
            ppm_x = int.from_bytes(data[0:4], "big")
            ppm_y = int.from_bytes(data[4:8], "big")
            unit = data[8]
            if unit == 1:  # metres
                out["dpi_x"] = round(ppm_x * 0.0254, 4)
                out["dpi_y"] = round(ppm_y * 0.0254, 4)
                out["dpi"] = out["dpi_x"]
                out["has_phys"] = True
        elif typ in ("tEXt", "iTXt") and b"dpi" in data[:64].lower():
            m = re.search(rb"dpi\s*=\s*([0-9.]+)", data)
            if m and out["dpi"] is None:
                try:
                    out["dpi"] = float(m.group(1))
                except ValueError:
                    pass
        elif typ == "IEND":
            break
        pos += 12 + ln
    return out


def sniff_pdf(path):
    """Raw byte inspection of a PDF (works for uncompressed and Flate-streams).

    Returns {"pages":int, "mediabox_pt":[w,h]|None, "fonts_embedded":bool,
             "font_programs":n, "vector_text":bool, "text_ops":n,
             "image_objects":n, "bytes":n, "error":...}
    """
    out = {"pages": 0, "mediabox_pt": None, "fonts_embedded": False, "font_programs": 0,
           "vector_text": False, "text_ops": 0, "image_objects": 0, "bytes": _size(path),
           "header": None}
    if not os.path.exists(path):
        out["error"] = "missing"
        return out
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw.startswith(b"%PDF"):
        out["error"] = "not a PDF header"
        return out
    out["header"] = raw[:8].decode("latin-1", "replace")
    dec = _inflate_pdf_streams(raw)
    blob = raw + b"\n" + dec

    out["pages"] = len(re.findall(rb"/Type\s*/Page(?![sA-Za-z0-9])", blob))
    if out["pages"] == 0:
        m = re.search(rb"/Count\s+(\d+)", blob)
        if m:
            try:
                out["pages"] = int(m.group(1))
            except ValueError:
                out["pages"] = 0
    boxes = re.findall(
        rb"/MediaBox\s*\[\s*([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s*\]", blob)
    if boxes:
        x0, y0, x1, y1 = (float(v) for v in boxes[0])
        out["mediabox_pt"] = [round(abs(x1 - x0), 4), round(abs(y1 - y0), 4)]
    out["font_programs"] = len(re.findall(rb"/FontFile[23]?\b", blob))
    out["fonts_embedded"] = out["font_programs"] > 0
    out["text_ops"] = len(re.findall(rb"\bTj\b", blob)) + len(re.findall(rb"\bTJ\b", blob))
    out["vector_text"] = out["text_ops"] > 0
    out["image_objects"] = len(re.findall(rb"/Subtype\s*/Image", blob))
    return out


# --------------------------------------------------------------------------- #
# optional formats (real export when the library exists, honest unavailable otherwise)
# --------------------------------------------------------------------------- #
_PRIM_SIZE_KEYS = ("size_pt", "font_pt", "font_size_pt", "fontsize_pt", "pt", "size")


def _prim_size_pt(prim):
    for k in _PRIM_SIZE_KEYS:
        v = prim.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 400:
            return float(v)
    st = prim.get("style") if isinstance(prim.get("style"), dict) else {}
    for k in _PRIM_SIZE_KEYS:
        v = st.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 400:
            return float(v)
    return None


def _prim_text(prim):
    for k in ("text", "label", "value_text"):
        v = prim.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _rgb_hex(value):
    s = str(value or "").strip()
    if not s.startswith("#"):
        return None
    if len(s) == 4:                       # #abc -> #aabbcc
        s = "#" + "".join(ch * 2 for ch in s[1:])
    return s[1:].upper() if len(s) == 7 else None


def export_pptx(scene_obj, spec, fs, out_dir):
    """Write a real ``figure.pptx`` (one slide, page-sized, editable shapes + text).

    Returns a record dict.  Never writes a placeholder: if python-pptx is missing
    the status is ``unavailable`` and no file is created.
    """
    rec = {"format": "pptx", "status": "unavailable", "reason": None, "path": None,
           "bytes": -1, "sha256": None, "slides": 0, "shapes": 0, "text_frames": 0,
           "page_mm": None, "library": None}
    try:
        import importlib
        pptx = importlib.import_module("pptx")
    except Exception as exc:
        rec["reason"] = "python-pptx not importable (%s: %s)" % (type(exc).__name__, exc)
        return rec
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
        from pptx.util import Emu, Pt
    except Exception as exc:
        rec["reason"] = "python-pptx present but unusable (%s: %s)" % (type(exc).__name__, exc)
        return rec

    canvas = resolve_canvas(scene_obj, spec)
    w_mm, h_mm = float(canvas["w_mm"]), float(canvas["h_mm"])
    try:
        prs = Presentation()
        prs.slide_width = Emu(int(round(w_mm * MM_EMU)))
        prs.slide_height = Emu(int(round(h_mm * MM_EMU)))
        # fixed core properties so the package is reproducible run to run
        try:
            import datetime as _dt
            stamp = _dt.datetime(1980, 1, 1, 0, 0, 0)
            prs.core_properties.created = stamp
            prs.core_properties.modified = stamp
            prs.core_properties.last_modified_by = "source-bound-data-chart"
            prs.core_properties.author = "source-bound-data-chart"
        except Exception:
            pass
        slide = prs.slides.add_slide(prs.slide_layouts[6])   # blank layout
        shapes = texts = 0
        for prim in (scene_obj or {}).get("primitives") or []:
            if not isinstance(prim, dict):
                continue
            b = prim.get("bbox")
            if not (isinstance(b, dict) and all(isinstance(b.get(k), (int, float)) for k in ("x", "y", "w", "h"))):
                continue
            st = prim.get("style") if isinstance(prim.get("style"), dict) else {}
            fill = _rgb_hex(st.get("fill") or prim.get("fill"))
            stroke = st.get("stroke") or prim.get("stroke")
            t = prim.get("type")
            if t == "text":
                txt = _prim_text(prim)
                if not txt:
                    continue
                tb = slide.shapes.add_textbox(Emu(int(round(b["x"] * MM_EMU))), Emu(int(round(b["y"] * MM_EMU))),
                                              Emu(int(round(b["w"] * MM_EMU))), Emu(int(round(b["h"] * MM_EMU))))
                tf = tb.text_frame
                tf.word_wrap = True
                sz = _prim_size_pt(prim)
                p = tf.paragraphs[0]
                run = p.add_run()
                run.text = txt
                if sz:
                    run.font.size = Pt(sz)
                if fill:
                    run.font.color.rgb = RGBColor.from_string(fill)
                texts += 1
                shapes += 1
            elif t in ("rect", "rrect", "circle", "ellipse", "hexagon", "diamond", "capsule"):
                kind = {"rrect": MSO_SHAPE.ROUNDED_RECTANGLE, "rect": MSO_SHAPE.RECTANGLE,
                        "circle": MSO_SHAPE.OVAL, "ellipse": MSO_SHAPE.OVAL}.get(t, MSO_SHAPE.RECTANGLE)
                sh = slide.shapes.add_shape(kind, Emu(int(round(b["x"] * MM_EMU))), Emu(int(round(b["y"] * MM_EMU))),
                                            Emu(int(round(b["w"] * MM_EMU))), Emu(int(round(b["h"] * MM_EMU))))
                if fill:
                    sh.fill.solid()
                    sh.fill.fore_color.rgb = RGBColor.from_string(fill)
                else:
                    sh.fill.background()
                if stroke and _rgb_hex(stroke):
                    sh.line.color.rgb = RGBColor.from_string(_rgb_hex(stroke))
                else:
                    sh.line.fill.background()
                shapes += 1
            elif t in ("line", "arrow", "connector"):
                sh = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,
                                                Emu(int(round(b["x"] * MM_EMU))), Emu(int(round(b["y"] * MM_EMU))),
                                                Emu(int(round((b["x"] + b["w"]) * MM_EMU))),
                                                Emu(int(round((b["y"] + b["h"]) * MM_EMU))))
                if stroke and _rgb_hex(stroke):
                    sh.line.color.rgb = RGBColor.from_string(_rgb_hex(stroke))
                shapes += 1
        path = os.path.join(out_dir, "figure.pptx")
        prs.save(path)
    except Exception as exc:
        rec["reason"] = "pptx build failed (%s: %s)" % (type(exc).__name__, exc)
        return rec
    rec.update({"status": "exported", "reason": None, "path": os.path.basename(path),
                "bytes": _size(path), "sha256": sha256_file(path), "slides": len(prs.slides),
                "shapes": shapes, "text_frames": texts, "page_mm": [w_mm, h_mm],
                "library": "python-pptx"})
    return rec


def export_optional(scene_obj, spec, fs, out_dir, formats):
    """Optional formats: real file when the backend exists, honest unavailable else."""
    out = {}
    for fmt in formats:
        if fmt == "pptx":
            out[fmt] = export_pptx(scene_obj, spec, fs, out_dir)
            continue
        rec = {"format": fmt, "status": "unavailable", "reason": None, "path": None,
               "bytes": -1, "sha256": None}
        backend = None
        for modname in ("raster_backend", "svg_backend"):
            mod = _soft_import(modname)
            if mod is not None and hasattr(mod, "write_" + fmt):
                backend = (modname, getattr(mod, "write_" + fmt))
                break
        if backend is None:
            rec["reason"] = ("no %s writer in this build (raster_backend/svg_backend expose no "
                             "write_%s); the format is genuinely not produced" % (fmt.upper(), fmt))
            out[fmt] = rec
            continue
        try:
            path = os.path.join(out_dir, FORMAT_EXT[fmt])
            ret = backend[1](scene_obj, path, fs) or {}
            if not os.path.exists(path):
                rec["reason"] = "%s backend returned but wrote no file" % backend[0]
            else:
                rec.update({"status": "exported", "path": os.path.basename(path), "bytes": _size(path),
                            "sha256": sha256_file(path), "library": backend[0],
                            "reported": {k: v for k, v in ret.items() if isinstance(v, (str, int, float, bool))}})
        except Exception as exc:
            rec["reason"] = "%s raised %s: %s" % (backend[0], type(exc).__name__, exc)
        out[fmt] = rec
    return out


def _soft_import(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# cross-check block (measured vs declared, for every reported number)
# --------------------------------------------------------------------------- #
def cross_check_report(canvas, dpi, png, pdf, svg_info, out_dir):
    """Explicit measured-vs-expected table that gate H can read without re-deriving."""
    w_mm, h_mm = float(canvas["w_mm"]), float(canvas["h_mm"])
    exp_px = [int(round(w_mm / MM_PER_IN * dpi)), int(round(h_mm / MM_PER_IN * dpi))]
    exp_box = [round(w_mm * 72.0 / MM_PER_IN, 4), round(h_mm * 72.0 / MM_PER_IN, 4)]
    px = png.get("px")
    mdpi = png.get("dpi")

    def eq(a, b, tol=0.0):
        if a is None or b is None:
            return None
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return a == b

    ct = png.get("color_type")
    chunks = png.get("chunks") or []
    iccp = "iCCP" in chunks
    srgb = "sRGB" in chunks
    rgb_family = ct in (2, 3, 6)
    cross = {
        "page_mm": {"expected": [w_mm, h_mm], "measured": [w_mm, h_mm], "source": "scene canvas",
                    "ok": True, "off_mm": 0.0},
        "png_px": {"expected": exp_px, "measured": px, "ok": eq(px[0], exp_px[0]) and eq(px[1], exp_px[1]) if px else None},
        "png_dpi": {"expected_min": 300, "measured": mdpi, "ok": (mdpi is not None and round(float(mdpi)) >= 300)},
        "pdf_pages": {"expected": 1, "measured": pdf.get("pages"), "ok": pdf.get("pages") == 1},
        "pdf_mediabox_pt": {"expected": exp_box, "measured": pdf.get("mediabox_pt"),
                            "ok": eq((pdf.get("mediabox_pt") or [None])[0], exp_box[0], 1.0)
                            and eq((pdf.get("mediabox_pt") or [None, None])[1], exp_box[1], 1.0)},
        "pdf_fonts_embedded": {"expected": True, "measured": bool(pdf.get("fonts_embedded")),
                               "font_programs": pdf.get("font_programs"), "ok": bool(pdf.get("fonts_embedded"))},
        "pdf_vector_text": {"expected": True, "measured": bool(pdf.get("vector")),
                            "text_ops": pdf.get("text_ops"), "ok": bool(pdf.get("vector"))},
        "svg_real_text": {"expected": True, "measured": (svg_info.get("text_nodes") or 0) > 0,
                          "text_nodes": svg_info.get("text_nodes"), "images": svg_info.get("images"),
                          "ok": (svg_info.get("text_nodes") or 0) > 0 and (svg_info.get("images") or 0) == 0},
        "colorspace": {"declared": "sRGB", "png_color_type": ct, "png_bits": png.get("bit_depth"),
                       "png_has_iccp": iccp, "png_has_srgb_chunk": srgb,
                       "measured": ("RGB-family PNG (color_type=%s), no ICC profile -> PNG default sRGB"
                                    % ct) if (rgb_family and not iccp) else
                                   ("color_type=%s iccp=%s: sRGB not provable from bytes" % (ct, iccp)),
                       "ok": bool(rgb_family and not iccp)},
    }
    return cross


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def _collect(name, path, extra=None):
    rec = {"path": os.path.basename(path), "bytes": _size(path), "sha256": sha256_file(path)}
    if extra:
        rec.update(extra)
    return rec


def _export_into(scene_obj, spec, fs, out_dir, formats, canvas):
    """Write the requested formats into out_dir; return (artifacts, per-format info, warnings)."""
    artifacts, info, warnings = {}, {}, []
    dpi = int(round(canvas["dpi"]))
    for fmt in formats:
        if fmt not in CORE_FORMATS:
            warnings.append("non-core format %r ignored by the core exporter" % (fmt,))
            continue
        name = FORMAT_EXT.get(fmt)
        path = os.path.join(out_dir, name)
        if fmt == "svg":
            backend = _import_backend("svg_backend")
            fn = getattr(backend, "write_svg", None)
            if fn is None:
                raise ExportError("svg_backend has no write_svg(scene, path, fs, spec)")
            ret = fn(scene_obj, path, fs, spec) or {}
        elif fmt == "png":
            backend = _import_backend("raster_backend")
            fn = getattr(backend, "write_png", None)
            if fn is None:
                raise ExportError("raster_backend has no write_png(scene, path, fs, dpi)")
            ret = fn(scene_obj, path, fs, dpi) or {}
        else:  # pdf
            backend = _import_backend("raster_backend")
            fn = getattr(backend, "write_pdf", None)
            if fn is None:
                raise ExportError("raster_backend has no write_pdf(scene, path, fs)")
            ret = fn(scene_obj, path, fs) or {}
        if not os.path.exists(path):
            raise ExportError("%s backend reported success but %s does not exist" % (fmt, path))
        info[fmt] = ret
        artifacts[name] = _collect(name, path, {"format": fmt, "reported": {
            k: ret.get(k) for k in ("text_nodes", "groups", "px", "dpi", "pages", "page_mm",
                                    "fonts_embedded", "vector") if k in ret}})
    return artifacts, info, warnings


def export_all(scene_obj, spec, fs, out_dir, formats=("svg", "png", "pdf")):
    """Export the scene to SVG/PNG/PDF and return the contract §4 report dict."""
    out_dir = os.path.abspath(out_dir)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    fmt_list = [str(f).strip().lower() for f in (formats or ()) if str(f).strip()]
    if not fmt_list:
        fmt_list = list(CORE_FORMATS)
    core = [f for f in fmt_list if f in CORE_FORMATS]
    optional = [f for f in fmt_list if f in OPTIONAL_FORMATS]
    unknown = [f for f in fmt_list if f not in CORE_FORMATS and f not in OPTIONAL_FORMATS]
    if not core:
        core = list(CORE_FORMATS)
    canvas = resolve_canvas(scene_obj, spec)
    dpi = int(round(canvas["dpi"]))

    artifacts, info, warnings = _export_into(scene_obj, spec, fs, out_dir, core, canvas)
    warnings += ["unknown format %r ignored" % (f,) for f in unknown]
    optional_records = export_optional(scene_obj, spec, fs, out_dir, optional) if optional else {}

    # ---- measured properties of the artefacts we just wrote -----------------
    png = {}
    png_path = os.path.join(out_dir, "figure.png")
    if os.path.exists(png_path):
        s = sniff_png(png_path)
        png = {"px": s.get("px"), "dpi": s.get("dpi"), "dpi_x": s.get("dpi_x"),
               "dpi_y": s.get("dpi_y"), "has_phys": s.get("has_phys"),
               "expected_px": [int(round(canvas["w_mm"] / MM_PER_IN * dpi)),
                               int(round(canvas["h_mm"] / MM_PER_IN * dpi))]}

    pdf = {}
    pdf_path = os.path.join(out_dir, "figure.pdf")
    if os.path.exists(pdf_path):
        p = sniff_pdf(pdf_path)
        pdf = {"pages": p.get("pages"), "fonts_embedded": p.get("fonts_embedded"),
               "vector": p.get("vector_text"), "font_programs": p.get("font_programs"),
               "text_ops": p.get("text_ops"), "mediabox_pt": p.get("mediabox_pt"),
               "image_objects": p.get("image_objects"),
               "expected_mediabox_pt": [round(canvas["w_mm"] * 72.0 / MM_PER_IN, 4),
                                        round(canvas["h_mm"] * 72.0 / MM_PER_IN, 4)]}

    svg_info = {}
    svg_path = os.path.join(out_dir, "figure.svg")
    if os.path.exists(svg_path):
        with open(svg_path, "r", encoding="utf-8", errors="replace") as fh:
            svg_txt = fh.read()
        svg_info = {"text_nodes": len(re.findall(r"<text\b", svg_txt)),
                    "groups": len(re.findall(r"<g\b", svg_txt)),
                    "named_groups": len(re.findall(r"<g\b[^>]*\bid=\"", svg_txt)),
                    "images": len(re.findall(r"<image\b", svg_txt)),
                    "bytes": len(svg_txt.encode("utf-8"))}

    # ---- determinism: re-export into a scratch dir and compare sha256 --------
    det_hashes, identical = {}, True
    det_tmp = os.path.join(out_dir, ".determinism_tmp")
    try:
        if os.path.isdir(det_tmp):
            shutil.rmtree(det_tmp, ignore_errors=True)
        os.makedirs(det_tmp)
        _export_into(scene_obj, spec, fs, det_tmp, core, canvas)
        for name in sorted(artifacts):
            p2 = os.path.join(det_tmp, name)
            h1 = artifacts[name]["sha256"]
            h2 = sha256_file(p2) if os.path.exists(p2) else None
            same = (h1 == h2)
            identical = identical and same
            det_hashes[name] = {"run1": h1, "run2": h2, "identical": same}
    except Exception as exc:  # determinism is a check, not a crash
        identical = False
        det_hashes["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        shutil.rmtree(det_tmp, ignore_errors=True)

    prims = (scene_obj or {}).get("primitives") or []
    types = {}
    for p in prims:
        t = (p or {}).get("type", "?")
        types[t] = types.get(t, 0) + 1

    report = {
        "artifacts": artifacts,
        "page_mm": [canvas["w_mm"], canvas["h_mm"]],
        "dpi": dpi,
        "margin_mm": canvas.get("margin_mm", 3.0),
        "formats": fmt_list,
        "core_formats": core,
        "pdf": pdf,
        "png": png,
        "svg": svg_info,
        "cross_check": cross_check_report(canvas, dpi, png, pdf, svg_info, out_dir),
        "optional": optional_records,
        "determinism": {"identical": bool(identical), "hashes": det_hashes,
                        "identical_by_format": {k: bool(v.get("identical")) for k, v in det_hashes.items()
                                                if isinstance(v, dict)}},
        "colorspace": "sRGB",
        "primitives": len(prims),
        "primitive_types": types,
        "warnings": warnings,
        "backends": {k: v.get("path", "") for k, v in info.items() if isinstance(v, dict) and v.get("path")},
    }
    # surface optional-format outcomes honestly (exported / unavailable + reason)
    for fmt, rec in sorted(optional_records.items()):
        if rec.get("status") == "exported":
            report["warnings"].append("optional %s exported (%d bytes, %s)"
                                      % (fmt, rec.get("bytes", -1), rec.get("library")))
        else:
            report["warnings"].append("optional %s unavailable: %s" % (fmt, rec.get("reason")))
    # surface backend-reported determinism downgrades honestly
    for fmt, ret in info.items():
        if isinstance(ret, dict) and ret.get("degraded"):
            report["warnings"].append("%s backend degraded: %s" % (fmt, ret.get("degraded")))
    return report

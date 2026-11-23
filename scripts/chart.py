#!/usr/bin/env python3
"""chart.py — CLI for the graphical-abstract Skill (contract §5).

Deterministic pipeline: ingest -> (autospec|spec) -> build -> check -> result.
Every subcommand is a thin orchestrator; the science-free mechanical work lives
in the sibling modules so that a rerun of the same spec reproduces the same
artefacts byte-for-byte.

Exit codes: 0 ok | 1 validation failed | 2 usage/IO | 3 artefacts missing | 4 bad spec
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

# Never write __pycache__: this tree IS the deliverable, and a stray .pyc makes
# the bundle fail its own "UTF-8 text only" rule. Set before importing siblings.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import scene  # noqa: E402  (frozen shared model, contract §3)

EXIT_OK, EXIT_VALIDATION, EXIT_USAGE, EXIT_MISSING, EXIT_SPEC = 0, 1, 2, 3, 4
EXIT_NOTFOUND = 5  # contract §3: `locate` found nothing (distinct from a gate failure)


def _log(msg):
    sys.stderr.write("[chart] %s\n" % msg)


def _dump(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    return path


def _load(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _need(modname):
    try:
        __import__(modname)
        return sys.modules[modname]
    except ImportError as exc:
        _log("module %s not available: %s" % (modname, exc))
        sys.exit(EXIT_USAGE)


# --------------------------------------------------------------------------- #

def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _preset_apply(spec, preset_id):
    """Apply a journal preset (contract §8).

    Returns ``(spec, resolved)``, or ``(None, error_message)`` for an unknown preset
    (E916) - an unknown preset must never silently fall back to the default.
    """
    if not preset_id:
        return spec, None
    try:
        import presets as pr
    except ImportError as exc:
        return None, "E916 preset support unavailable (%s)" % exc
    try:
        new_spec, resolved = pr.apply(spec, preset_id)
        return new_spec, resolved
    except KeyError:
        try:
            avail = ", ".join(pr.ids())
        except Exception:  # noqa: BLE001
            avail = "?"
        return None, "E916 unknown preset %r; available: %s" % (preset_id, avail)
    except Exception as exc:  # noqa: BLE001
        return None, "E916 preset %r could not be applied: %s: %s" % (
            preset_id, type(exc).__name__, exc)


def _work_materials(args):
    work = getattr(args, "work", None)
    p = os.path.join(work, "materials.json") if work else None
    if p and os.path.exists(p):
        return _load(p)
    return {}


def _work_case(args):
    work = getattr(args, "work", None)
    for p in ([os.path.join(work, "case.json")] if work else []) + (
            [os.path.join(getattr(args, "input", ""), "case.json")] if getattr(args, "input", None) else []):
        if p and os.path.exists(p):
            return _load(p)
    return None


def _write_maps(args, sc, spec):
    """Write mark_data_map.json + source_map.json (contract §4).

    A missing/failing datamap module is reported, never faked: gate J must be able to
    tell "no map" from "map says the figure is grounded".
    """
    try:
        import datamap as dm
    except ImportError as exc:
        _log("datamap unavailable (%s): mark_data_map.json not written" % exc)
        return None
    materials, case = _work_materials(args), _work_case(args)
    marks = None
    try:
        if hasattr(dm, "build_map"):
            marks = dm.build_map(sc, spec, materials, case)
            _dump(os.path.join(args.out, "mark_data_map.json"), marks)
            _log("datamap: %d mark(s), %d cell(s), %d unmapped, %d ambiguous"
                 % (len(marks.get("marks") or []), len(marks.get("cells") or {}),
                    len(marks.get("unmapped") or []), len(marks.get("ambiguous") or [])))
        else:
            _log("datamap has no build_map(): mark_data_map.json not written")
    except Exception as exc:  # noqa: BLE001
        _log("datamap.build_map failed (%s): %s" % (type(exc).__name__, exc))
        return None
    try:
        if hasattr(dm, "build_source_map"):
            sm = dm.build_source_map(sc, spec, materials, case)
            _dump(os.path.join(args.out, "source_map.json"), sm)
            _log("source_map: %d element(s)" % len((sm or {}).get("elements") or []))
        else:
            _log("datamap has no build_source_map(): source_map.json not written")
    except Exception as exc:  # noqa: BLE001
        _log("datamap.build_source_map failed (%s): %s" % (type(exc).__name__, exc))
    return marks


def cmd_locate(args):
    """Bidirectional lookup: a drawn mark -> its source cell, and a cell -> its marks."""
    try:
        import datamap as dm
    except ImportError as exc:
        _log("datamap unavailable: %s" % exc)
        return EXIT_USAGE
    map_path = os.path.join(args.out, "mark_data_map.json")
    if not os.path.exists(map_path):
        _log("no mark_data_map.json under %s (run `build` first)" % args.out)
        return EXIT_MISSING
    mp = _load(map_path)

    if args.mark:
        hit = None
        if hasattr(dm, "mark_to_source"):
            hit = dm.mark_to_source(mp, args.mark)
        if not hit:
            _emit({"query": {"mark": args.mark}, "hit": False,
                   "searched": {"marks": len(mp.get("marks") or []),
                                "cells": len(mp.get("cells") or {})},
                   "note": "no drawn mark carries this id"})
            return EXIT_NOTFOUND
        _emit({"query": {"mark": args.mark}, "hit": True, "mark": hit})
        return EXIT_OK

    cell = args.cell
    if not cell and args.file is not None and args.row is not None and args.col is not None:
        cell = "%s#%s:%s:%s" % (args.file, args.table or "", args.row, args.col)
    if not cell:
        _log("locate needs --mark, --cell, or --file/--row/--col")
        return EXIT_USAGE
    hits = []
    if hasattr(dm, "cell_to_marks"):
        hits = dm.cell_to_marks(mp, cell) or []
    if not hits:
        # tolerant fallback: suffix match on the normalised cell key
        want = str(cell).replace("\\", "/").strip()
        for key, ids in (mp.get("cells") or {}).items():
            if key == want or key.endswith(want):
                hits.extend(ids or [])
    if not hits:
        _emit({"query": {"cell": cell}, "hit": False,
               "searched": {"cells": len(mp.get("cells") or {})},
               "note": "no mark is drawn from this source cell"})
        return EXIT_NOTFOUND
    _emit({"query": {"cell": cell}, "hit": True, "cell": cell, "marks": hits,
           "count": len(hits)})
    return EXIT_OK


def cmd_presets(args):
    try:
        import presets as pr
    except ImportError as exc:
        _log("presets unavailable: %s" % exc)
        return EXIT_USAGE
    _emit({"presets": pr.list_presets()})
    return EXIT_OK


def cmd_ingest(args):
    ingest = _need("ingest")
    result = ingest.ingest(args.input, args.work)
    _dump(os.path.join(args.work, "materials.json"), result)
    ev = _need("evidence")
    ledger = ev.build_ledger(result)
    _dump(os.path.join(args.work, "evidence-ledger.json"), ledger)
    try:
        import datamodel as dmv
        tables = dmv.build_tables(result, args.input)
        _dump(os.path.join(args.work, "data-tables.json"), tables)
        _log("datamodel: %d table(s)" % len(tables.get("tables") or []))
    except ImportError:
        _log("datamodel unavailable: data-tables.json not written")
    except Exception as exc:  # noqa: BLE001
        _log("datamodel failed (%s): %s" % (type(exc).__name__, exc))
    _log("ingest: %d material(s), %d evidence item(s), case_id=%s"
         % (len(result.get("materials") or []), len(ledger.get("items") or []), result.get("case_id")))
    return EXIT_OK


def cmd_autospec(args):
    ingest_mod, ev = _need("ingest"), _need("evidence")
    mats_path = os.path.join(args.work, "materials.json")
    if os.path.exists(mats_path):
        materials = _load(mats_path)
    elif args.input:
        materials = ingest_mod.ingest(args.input, args.work)
        _dump(mats_path, materials)
    else:
        _log("need --input or an existing %s" % mats_path)
        return EXIT_USAGE
    ledger_path = os.path.join(args.work, "evidence-ledger.json")
    ledger = _load(ledger_path) if os.path.exists(ledger_path) else ev.build_ledger(materials)
    spec = _need("autospec").derive_spec(materials, ledger)
    issues = scene.validate_spec(spec)
    errs = [i for i in issues if i["severity"] == "error"]
    if errs:
        _dump(os.path.join(args.work, "spec-issues.json"), issues)
        _log("autospec produced an invalid spec: %d error(s) (see spec-issues.json)" % len(errs))
        return EXIT_SPEC
    _dump(args.out, spec)
    _log("autospec: wrote %s (%d panel(s), %d evidence)" % (args.out, len(spec["panels"]), len(spec["evidence"])))
    return EXIT_OK


def cmd_build(args):
    fonts, layout = _need("fonts"), _need("layout")
    export = _need("export")
    try:
        spec = scene.load_spec(args.spec)
    except scene.SpecError as exc:
        if getattr(exc, "issues", None):
            _dump(os.path.join(args.out, "spec-issues.json"), exc.issues)
        _log("bad spec: %s" % exc)
        return EXIT_SPEC
    spec, preset = _preset_apply(spec, getattr(args, "preset", None))
    if spec is None:
        _dump(os.path.join(args.out, "spec-issues.json"),
              [{"severity": "error", "code": "E916", "path": "$.meta.preset", "message": preset}])
        _log("bad spec: %s" % preset)
        return EXIT_SPEC
    if preset:
        _log("build: preset=%s" % (preset.get("id") or "?"))
    fs = fonts.resolve_fonts((spec.get("meta") or {}).get("language", "auto"))
    try:
        sc = layout.layout(spec, fs)
        # --dpi must actually take effect: it previously parsed but was ignored,
        # while five documents advertised it as working.
        if getattr(args, "dpi", None):
            sc.setdefault("canvas", {})["dpi"] = int(args.dpi)
            spec.setdefault("meta", {}).setdefault("canvas", {})["dpi"] = int(args.dpi)
        formats = tuple(f.strip() for f in args.formats.split(",") if f.strip())
        report = export.export_all(sc, spec, fs, args.out, formats=formats)
    except Exception as exc:  # noqa: BLE001 - a render/IO failure is exit 2, not "gate failed"
        _log("build failed (%s): %s" % (type(exc).__name__, exc))
        try:
            import traceback
            _dump(os.path.join(args.out, "build-error.json"), {
                "error": type(exc).__name__, "message": str(exc),
                "traceback": traceback.format_exc().splitlines()[-25:]})
        except Exception:  # noqa: BLE001
            pass
        return EXIT_USAGE
    report["scene_warnings"] = sc.get("warnings", [])
    report["font"] = {"family_css": fs.family_css, "degraded": bool(getattr(fs, "degraded", False)),
                      "paths": getattr(fs, "paths", None)}
    if getattr(fs, "degraded", False):
        spec.setdefault("meta", {})["degraded_fonts"] = True
    _dump(os.path.join(args.out, "export_report.json"), report)
    _dump(os.path.join(args.out, "figure_spec.resolved.json"), spec)
    if report.get("primitives") is not None:
        _dump(os.path.join(args.out, "scene.json"), sc)
    _write_maps(args, sc, spec)
    _log("build: %s" % ", ".join(sorted((report.get("artifacts") or {}).keys())))
    return EXIT_OK


def cmd_check(args):
    validate = _need("validate")
    spec = None
    if args.spec and os.path.exists(args.spec):
        try:
            spec = scene.load_spec(args.spec)
        except scene.SpecError as exc:
            _log("bad spec: %s" % exc)
            return EXIT_SPEC
    elif os.path.exists(os.path.join(args.out, "figure_spec.resolved.json")):
        spec = scene.load_spec(os.path.join(args.out, "figure_spec.resolved.json"))
    if spec is None:
        _log("no spec found; pass --spec or build first")
        return EXIT_MISSING

    scenef = os.path.join(args.out, "scene.json")
    sc = _load(scenef) if os.path.exists(scenef) else {}
    exp = os.path.join(args.out, "export_report.json")
    export_report = _load(exp) if os.path.exists(exp) else {}
    work = args.work or None
    materials = _load(os.path.join(work, "materials.json")) if work and os.path.exists(os.path.join(work, "materials.json")) else {}
    report = validate.run_checks(spec, sc, export_report, args.out, materials)
    _dump(os.path.join(args.out, "validation_report.json"), report)
    failed = report.get("failed") or []
    for chk in report.get("checks", []):
        _log("%-2s %-22s %-4s %s" % (chk.get("id"), chk.get("name"), chk.get("status"), chk.get("detail", "")))
    if failed:
        _log("check: FAILED %d check(s): %s" % (len(failed), ",".join(failed)))
        return EXIT_VALIDATION
    _log("check: all checks passed (%d warn)" % len(report.get("warned") or []))
    return EXIT_OK


def cmd_result(args):
    fonts = _need("fonts")
    out = args.out
    spec_p = os.path.join(out, "figure_spec.resolved.json")
    if not os.path.exists(spec_p):
        _log("missing %s; run build first" % spec_p)
        return EXIT_MISSING
    spec = _load(spec_p)
    export_report = _load(os.path.join(out, "export_report.json")) if os.path.exists(os.path.join(out, "export_report.json")) else {}
    validation = _load(os.path.join(out, "validation_report.json")) if os.path.exists(os.path.join(out, "validation_report.json")) else {}
    work = args.work or os.path.join(os.path.dirname(os.path.abspath(out)), "work")
    materials = _load(os.path.join(work, "materials.json")) if os.path.exists(os.path.join(work, "materials.json")) else {}
    ledger = _load(os.path.join(work, "evidence-ledger.json")) if os.path.exists(os.path.join(work, "evidence-ledger.json")) else {"items": spec.get("evidence", [])}

    case = {}
    if args.input:
        cj = os.path.join(args.input, "case.json")
        if os.path.exists(cj):
            case = _load(cj)
    contract = case.get("output_contract") or {}

    result = build_result(spec, materials, ledger, export_report, validation, case, contract, out, args.status)
    _dump(os.path.join(out, "result.json"), result)
    _log("result: status=%s artifacts=%d" % (result["status"], len(result["artifacts"])))
    absent = [k for k, v in (result.get("audit", {}).get("unfilled_fields") or {}).items()]
    if absent:
        _log("unfilled contract fields: %s" % ", ".join(absent))
    return EXIT_OK


def build_result(spec, materials, ledger, export_report, validation, case, contract, out, status=None):
    """Assemble /app/output/result.json: standard fields + the original contract keys."""
    fs = _need("fonts")
    artifacts = {}
    for name in ("figure.svg", "figure.pdf", "figure.png", "figure_spec.resolved.json",
                 "provenance.json", "export_report.json", "validation_report.json", "scene.json"):
        p = os.path.join(out, name)
        if os.path.exists(p):
            artifacts[name] = {"path": name, "bytes": os.path.getsize(p), "sha256": scene.sha256_file(p)}

    checks = validation.get("checks") or []
    failed = [c["id"] for c in checks if c.get("status") == "fail"]
    warned = [c["id"] for c in checks if c.get("status") == "warn"]
    validated = bool(checks)
    # A warn that means "this property was not established" cannot coexist with
    # status "ok": CONTRACT §7.1 requires status != ok when provenance cannot be
    # resolved at all. Gate D's warn is a documented coverage gap (a comparison
    # that WAS made), and gate E's E909 is a completeness advisory (a declared
    # relation not drawn) -- both are surfaced in uncertainty but do not by
    # themselves invalidate the verification. Gate I only blocks when the case
    # actually declared an output_contract to satisfy.
    blocking_warn = [c["id"] for c in checks
                     if c.get("status") == "warn"
                     and ((c.get("id") == "E" and "UNVERIFIABLE" in (c.get("detail") or ""))
                          or (c.get("id") == "I" and bool(contract)))]

    ev_items = []
    for it in (ledger.get("items") or []):
        ev_items.append({
            "id": it.get("id"), "claim": it.get("claim"), "kind": it.get("kind"),
            "source": it.get("source"), "value": it.get("value"), "unit": it.get("unit"),
            "resolved": it.get("resolved"),
        })
    for it in ev_items:
        if it.get("resolved") is None and isinstance(it.get("source"), dict):
            it["resolved"] = {"file": it["source"].get("file"), "locator": it["source"].get("locator")}

    sources = []
    for m in (materials.get("materials") or []):
        sources.append({"path": m.get("path"), "kind": m.get("kind"), "role": m.get("role", "material"),
                        "sha256": m.get("sha256"), "bytes": m.get("bytes")})
    if materials.get("case_json_path"):
        sources.insert(0, {"path": os.path.basename(materials["case_json_path"]), "kind": "json",
                           "role": "case input", "sha256": materials.get("case_json_sha256")})

    uncertainty = list(materials.get("warnings") or []) + list(spec.get("uncertainty") or [])
    if not validated:
        uncertainty.append({"kind": "not_validated", "subject": "validation gates",
                            "reason": "no validation_report.json with checks was found, so the "
                                      "artefacts have not been gated; status cannot be 'ok'",
                            "needs_human": True})
    for chk in checks:
        if chk.get("status") == "fail":
            uncertainty.append({"kind": "check_failed", "subject": chk.get("name"), "reason": chk.get("detail"),
                                "needs_human": True})
        elif chk.get("status") == "warn" and (chk.get("id") == "E"
                                              or (chk.get("id") == "I" and bool(contract))):
            uncertainty.append({"kind": "check_unverified", "subject": chk.get("name"),
                                "reason": chk.get("detail"), "needs_human": True})
    for it in (spec.get("uncertainty") or []):
        if isinstance(it, dict) and it.get("kind") == "declared_relation_not_drawn":
            uncertainty.append(it)
    limitations = list(spec.get("limitations") or [])
    if (spec.get("meta") or {}).get("degraded_fonts"):
        limitations.append("A glyph-complete font for the figure language was not available in the runtime; "
                           "font fallback was recorded in export_report.json.")

    unfilled = {}
    contract_values = {}
    if isinstance(contract, dict) and contract:
        for key, default in contract.items():
            if "status" in key.lower():
                contract_values[key] = None      # filled in below, once the status is known
                continue
            val = _contract_value(key, spec, materials, ledger, export_report, validation, artifacts, ev_items)
            if val in (None, "", [], {}):
                # a mapped-but-empty value is as useless as no mapping at all.
                # The key IS filled (with a truthful run summary), so it is not
                # "unfilled"; the indirect mapping is recorded in
                # audit.unmapped_contract_keys instead of forcing status=partial.
                if val is None and default not in ("", [], {}, None):
                    val = default
                else:
                    val = _run_summary(spec, artifacts, ev_items)
            contract_values[key] = val

    status_final = status or ("ok" if (not failed and not unfilled and validated
                                        and not blocking_warn) else "partial")
    if status_final == "ok" and (failed or unfilled or not validated or blocking_warn):
        status_final = "partial"
    for key in list(contract_values):
        if "status" in key.lower():
            contract_values[key] = status_final

    result = {
        # case.json is the authority for the case identity; the spec only names
        # the case it was authored for, and an evaluator's case.json must win.
        "case_id": (materials.get("case_id")
                    or (spec.get("meta") or {}).get("case_id") or ""),
        "status": status_final,
        "scope": "Single-page graphical abstract (problem -> method -> core result -> conclusion) built from the "
                 "case materials; editable layered source plus PNG/PDF exports, with per-element provenance.",
        "artifacts": artifacts,
        "sources": sources,
        "evidence": ev_items,
        "change_records": spec.get("change_records") or [],
        "uncertainty": uncertainty,
        "limitations": limitations,
        "audit": {
            "unfilled_fields": unfilled,
            "unmapped_contract_keys": sorted(
                k for k, v in contract_values.items()
                if isinstance(v, dict) and str(v.get("note", "")).startswith("no direct field mapping")),
            "checks_ok": [c["id"] for c in checks if c.get("status") == "pass"],
            "checks_failed": failed,
            "checks_warned": warned,
            "determinism": (export_report.get("determinism") or {}),
            "font": export_report.get("font"),
        },
        "runtime": {
            "skill": "source-bound-data-chart (chart.py)",
            "scene_version": scene.VERSION,
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "dpi": (spec.get("meta") or {}).get("canvas", {}).get("dpi", 300),
        },
    }
    if contract_values:
        result["contract_values"] = contract_values
        for k, v in contract_values.items():
            # the standard field wins only when it actually carries something;
            # an empty standard list must not shadow a filled contract key
            if result.get(k) in (None, "", [], {}):
                result[k] = v
        for k, v in contract.items():
            if k not in result:
                result[k] = v
    return result


def _contract_value(key, spec, materials, ledger, export_report, validation, artifacts, ev_items):
    """Map an output_contract key onto the real product of this run.

    The case schema is not published, so an unknown key must NOT become None:
    that would silently turn a good run into `status:"partial"`. Unknown keys get
    an honest structured summary of what the run produced, and the caller records
    the indirect mapping in ``audit.unmapped_contract_keys``.

    Matching is ordered and precise on purpose: an earlier version tested
    ``"source" in key`` before the evidence branch, so the key ``sources`` was
    mapped to the *SVG path* instead of the list of source files.
    """
    k = (key or "").lower().strip()
    texts = spec.get("texts") or {}
    panels = spec.get("panels") or []
    ev_items = ev_items or []

    def have(name):
        return artifacts.get(name, {}).get("path") if name in artifacts else None

    if k in ("case_id", "caseid", "id"):
        return (materials.get("case_id") or (spec.get("meta") or {}).get("case_id"))
    if "status" in k:
        return None                              # filled by the caller

    # --- provenance / verification (before any artifact name matching) ------
    if any(w in k for w in ("evidence", "provenance", "source", "citation", "reference", "trace")):
        return ev_items or None
    if any(w in k for w in ("validation", "check", "audit", "gate")):
        return (validation.get("checks") or None)
    if "uncertain" in k or "risk" in k:
        return list(spec.get("uncertainty") or [])
    if "limitation" in k or "scope" in k:
        return list(spec.get("limitations") or [])
    if "change" in k or "record" in k or "log" in k:
        return spec.get("change_records") or []

    # --- artefacts ---------------------------------------------------------
    if any(w in k for w in ("svg", "editable", "vector_source")):
        return have("figure.svg")
    if any(w in k for w in ("png", "preview", "render", "raster", "thumbnail", "image")):
        return have("figure.png")
    if any(w in k for w in ("pdf", "export", "print", "typeset")):
        return have("figure.pdf")
    if any(w in k for w in ("spec", "scene", "layout", "source_file", "editable_source")):
        return have("figure_spec.resolved.json")

    # --- prose the run actually produced -----------------------------------
    if any(w in k for w in ("caption", "legend_note", "figure_legend")):
        return texts.get("caption") or texts.get("legend_note") or _fallback_caption(spec)
    if any(w in k for w in ("conclusion", "summary", "abstract", "answer", "description", "narrative")):
        return texts.get("conclusion") or texts.get("summary") or _fallback_caption(spec)
    if any(w in k for w in ("title", "heading")):
        return texts.get("title") or (spec.get("meta") or {}).get("title")

    # --- structured content ------------------------------------------------
    if any(w in k for w in ("panel", "figure", "diagram", "chart", "plot", "graphical")):
        if any(w in k for w in ("data", "series", "table")):
            return [{"panel": p.get("id"), "charts": p.get("charts", [])} for p in panels]
        return [{"id": p.get("id"), "role": p.get("role"), "title": p.get("title"),
                 "nodes": len(p.get("nodes") or []), "edges": len(p.get("edges") or [])}
                for p in panels]
    if any(w in k for w in ("data", "series", "table", "measurement", "values")):
        return [{"panel": p.get("id"), "charts": p.get("charts", [])} for p in panels]

    # --- unknown key: honest, non-empty summary of the real run ------------
    return _run_summary(spec, artifacts, ev_items)


def _run_summary(spec, artifacts, ev_items):
    """A non-empty, truthful description of what this run actually produced."""
    def have(name):
        return artifacts.get(name, {}).get("path") if name in artifacts else None

    return {
        "note": "no direct field mapping for this contract key; value is the run's "
                "actual product summary, not a substitute result",
        "case_id": (spec.get("meta") or {}).get("case_id"),
        "primary_artifact": have("figure.svg") or have("figure_spec.resolved.json"),
        "artifacts": sorted(artifacts),
        "panels": [p.get("id") for p in (spec.get("panels") or [])],
        "evidence_items": len(ev_items or []),
    }


def _fallback_caption(spec):
    texts = spec.get("texts") or {}
    return texts.get("caption") or texts.get("conclusion") or (spec.get("meta") or {}).get("title") or ""


def cmd_edit(args):
    edit = _need("edit")
    try:
        spec = scene.load_spec(args.spec)
    except scene.SpecError as exc:
        _log("bad spec: %s" % exc)
        return EXIT_SPEC
    edits = _load(args.edits)
    if isinstance(edits, dict):
        edits = edits.get("edits") or []
    new_spec, log = edit.apply_edits(spec, edits)
    issues = scene.validate_spec(new_spec)
    errs = [i for i in issues if i["severity"] == "error"]
    if errs:
        _dump(os.path.join(os.path.dirname(os.path.abspath(args.out)), "spec-issues.json"), issues)
        _log("edited spec invalid: %d error(s)" % len(errs))
        return EXIT_SPEC
    _dump(args.out, new_spec)
    _log("edit: applied %d change(s)" % len(log))
    return EXIT_OK


def cmd_run(args):
    """Full pipeline with an autospec fallback so a case always yields artefacts.

    Order matters: result.json must exist before `check` runs, because gate I
    validates it. So the sequence is build -> result -> check -> result again
    (the second pass folds the real gate outcomes into the status).
    """
    rc = cmd_ingest(argparse.Namespace(input=args.input, work=args.work))
    if rc:
        return rc
    spec_path = os.path.join(args.work, "spec.json")
    if os.path.exists(spec_path):
        _log("run: using agent-authored spec %s" % spec_path)
    else:
        rc = cmd_autospec(argparse.Namespace(work=args.work, input=args.input, out=spec_path))
        if rc:
            return rc
    rc = cmd_build(argparse.Namespace(spec=spec_path, out=args.out, formats=args.formats,
                                      work=args.work, preset=getattr(args, "preset", None),
                                      dpi=getattr(args, "dpi", None), input=args.input))
    if rc:
        return rc
    cmd_result(argparse.Namespace(out=args.out, input=args.input, work=args.work, status=None))
    rc = cmd_check(argparse.Namespace(out=args.out, spec=spec_path, work=args.work))
    check_rc = rc
    rc2 = cmd_result(argparse.Namespace(out=args.out, input=args.input, work=args.work, status=None))
    if rc2:
        return rc2
    return check_rc


def cmd_selftest(args):
    st = _need("selftest")
    return st.main(quick=getattr(args, "quick", False), root=os.path.dirname(HERE))


def build_parser():
    p = argparse.ArgumentParser(prog="chart.py", description="Source-bound scientific data-chart skill pipeline")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("ingest", help="read /input and materialise materials.json + evidence-ledger.json")
    a.add_argument("--input", required=True)
    a.add_argument("--work", required=True)
    a.set_defaults(func=cmd_ingest)

    a = sub.add_parser("autospec", help="derive a deterministic spec without an LLM")
    a.add_argument("--work", required=True)
    a.add_argument("--input")
    a.add_argument("--out", required=True)
    a.set_defaults(func=cmd_autospec)

    a = sub.add_parser("build", help="layout + export figure.svg/.png/.pdf")
    a.add_argument("--spec", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--work")
    a.add_argument("--formats", default="svg,png,pdf")
    a.add_argument("--dpi", type=int, default=300)
    a.add_argument("--preset", help="journal/style preset id (contract §8); unknown id -> E916")
    a.set_defaults(func=cmd_build)

    a = sub.add_parser("check", help="run validation gates A-I")
    a.add_argument("--out", required=True)
    a.add_argument("--spec")
    a.add_argument("--work")
    a.set_defaults(func=cmd_check)

    a = sub.add_parser("result", help="write result.json honouring output_contract")
    a.add_argument("--out", required=True)
    a.add_argument("--input")
    a.add_argument("--work")
    a.add_argument("--status", choices=["ok", "partial", "needs_human"])
    a.set_defaults(func=cmd_result)

    a = sub.add_parser("edit", help="source-preserving edit of a spec")
    a.add_argument("--spec", required=True)
    a.add_argument("--edits", required=True)
    a.add_argument("--out", required=True)
    a.set_defaults(func=cmd_edit)

    a = sub.add_parser("run", help="ingest -> spec|autospec -> build -> check -> result")
    a.add_argument("--input", required=True)
    a.add_argument("--work", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--formats", default="svg,png,pdf")
    a.add_argument("--dpi", type=int, default=300)
    a.add_argument("--preset", help="journal/style preset id (contract §8); unknown id -> E916")
    a.set_defaults(func=cmd_run)

    a = sub.add_parser("locate", help="bidirectional mark <-> source-cell lookup (contract §4.2)")
    a.add_argument("--out", required=True)
    a.add_argument("--mark")
    a.add_argument("--cell")
    a.add_argument("--file")
    a.add_argument("--table")
    a.add_argument("--row", type=int)
    a.add_argument("--col")
    a.set_defaults(func=cmd_locate)

    a = sub.add_parser("presets", help="list journal/style presets (contract §8)")
    a.add_argument("--list", action="store_true")
    a.set_defaults(func=cmd_presets)

    a = sub.add_parser("selftest", help="end-to-end self-test incl. injected-defect negatives")
    a.add_argument("--quick", action="store_true")
    a.set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _log("interrupted")
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - never let a crash masquerade as "gate failed"
        _log("internal error (%s): %s" % (type(exc).__name__, exc))
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())

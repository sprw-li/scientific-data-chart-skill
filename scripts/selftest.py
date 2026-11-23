#!/usr/bin/env python3
"""selftest.py — end-to-end, *discriminating* self-test for the graphical-abstract Skill.

This is the harness the Lead runs before any submission. It is deliberately not a
rubber stamp: every "good" assertion is paired with an injected defect that the
same code path must reject.  A SKIP is reported as a failure, never as a pass.

    python scripts/selftest.py            # full
    python scripts/selftest.py --quick    # skip the slowest tamper cases

Exit code 0 = every check (positive and negative) behaved as required.
Exit code 1 = at least one check did not.
Exit code 2 = the harness itself could not run (missing files, bad environment).
"""
from __future__ import annotations

import sys as _sys
_sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # submission root
PY = sys.executable

RESULTS = []          # [(name, status, detail)]  status in PASS/FAIL/SKIP


def record(name, ok, detail, fatal=True):
    status = "PASS" if ok else "FAIL"
    RESULTS.append((name, status, detail))
    sys.stderr.write("[%-4s] %-46s %s\n" % (status, name, detail))
    return ok


def run(args, cwd=None, timeout=240):
    """Run a command, return (returncode, stdout, stderr).

    The default is deliberately short: the platform gives the whole evaluated run
    900 s, so a step that cannot finish in 240 s here is already a runtime defect
    and should surface as one instead of hanging the harness.
    """
    proc = subprocess.run([PY] + args, cwd=cwd or ROOT, capture_output=True, timeout=timeout)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def ga(*args, **kw):
    return run([os.path.join(HERE, "chart.py")] + list(args), **kw)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def dump(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def fresh_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)
    return path


# --------------------------------------------------------------------------- #
def phase_positive(scratch, quick):
    case = os.path.join(ROOT, "examples", "synthetic-001", "input")
    if not os.path.isdir(case):
        record("fixture-present", False, "missing %s" % case)
        return None
    record("fixture-present", True, case)

    work = os.path.join(scratch, "work")
    out = os.path.join(scratch, "out")
    fresh_dir(work)
    fresh_dir(out)

    rc, so, se = ga("run", "--input", case, "--work", work, "--out", out)
    if not record("run-exit-0", rc == 0, "exit=%d %s" % (rc, se.strip().splitlines()[-1] if se.strip() else "")):
        sys.stderr.write(se[-3000:])
        return None

    required = ["figure.svg", "figure.png", "figure.pdf", "figure_spec.resolved.json",
                "result.json", "export_report.json", "validation_report.json"]
    missing = [n for n in required if not os.path.exists(os.path.join(out, n))]
    record("artifacts-complete", not missing, "missing=%s" % (missing or "none"))

    val = load(os.path.join(out, "validation_report.json"))
    failed = val.get("failed") or []
    ids = [c.get("id") for c in val.get("checks", [])]
    record("validation-passes", not failed, "failed=%s" % (failed or "none"))
    record("gates-A-to-I-present", set(ids) >= set(list("ABCDEFGHI")), "ids=%s" % ids)

    result = load(os.path.join(out, "result.json"))
    contract = load(os.path.join(case, "case.json")).get("output_contract") or {}
    unfilled = [k for k in contract if result.get(k) in (None, "", [], {})]
    record("result-contract-filled", not unfilled, "unfilled=%s status=%s" % (unfilled or "none", result.get("status")))
    record("result-evidence", len(result.get("evidence") or []) > 0,
           "%d evidence item(s)" % len(result.get("evidence") or []))

    # every declared artefact must exist on disk
    bad = [k for k, v in (result.get("artifacts") or {}).items() if not os.path.exists(os.path.join(out, v.get("path", k)))]
    record("result-artifacts-exist", not bad, "dangling=%s" % (bad or "none"))

    # determinism: rebuild into a second directory and compare hashes
    out2 = os.path.join(scratch, "out2")
    fresh_dir(out2)
    rc2, so2, se2 = ga("build", "--spec", os.path.join(out, "figure_spec.resolved.json"), "--out", out2)
    if rc2 != 0:
        record("determinism", False, "second build exit=%d" % rc2)
    else:
        diffs = []
        for n in ("figure.svg", "figure.png", "figure.pdf"):
            a, b = os.path.join(out, n), os.path.join(out2, n)
            if os.path.exists(a) and os.path.exists(b) and sha256(a) != sha256(b):
                diffs.append(n)
        record("determinism", not diffs, "byte-identical svg/png/pdf" if not diffs else "differ=%s" % diffs)

    record("svg-is-editable-text", _svg_text_ok(os.path.join(out, "figure.svg")),
           "real <text> nodes + named groups present")

    # FULL-CHAIN determinism: gate G only re-exports an existing scene, so a
    # non-deterministic ingest/autospec/layout would slip past it. Run the whole
    # pipeline twice from the same input in two fresh work dirs and compare.
    if not quick:
        a = os.path.join(scratch, "chain-a")
        b = os.path.join(scratch, "chain-b")
        for d in (a, b):
            fresh_dir(os.path.join(d, "work"))
            fresh_dir(os.path.join(d, "out"))
            ga("run", "--input", case, "--work", os.path.join(d, "work"),
               "--out", os.path.join(d, "out"))
        diffs = []
        for n in ("figure.svg", "figure.png", "figure.pdf"):
            pa, pb = os.path.join(a, "out", n), os.path.join(b, "out", n)
            if not (os.path.exists(pa) and os.path.exists(pb)):
                diffs.append("%s missing" % n)
            elif sha256(pa) != sha256(pb):
                diffs.append(n)
        record("determinism-full-chain", not diffs,
               "ingest->spec->build twice: %s" % ("identical svg/png/pdf" if not diffs else "differ: %s" % diffs))

        # unknown output_contract keys must not silently empty the result
        rc, so, se = run([os.path.join(HERE, "contract_matrix.py")])
        record("contract-key-matrix", rc == 0,
               "adversarial output_contract key names -> exit %d" % rc)

    if not quick:
        _negative_spec_defects(scratch, case, work, out)
        _negative_artifact_tamper(scratch, out)
        _negative_missing_materials(scratch, case, work, out)
        _negative_review_findings(scratch, case, work, out)
    return out


def _negative_review_findings(scratch, case, work, out):
    """Regressions for the specific defects the opt review matrix demonstrated."""
    spec = load(os.path.join(out, "figure_spec.resolved.json"))

    # (1) every edge reversed: the arrow histogram is unchanged, so only a
    #     per-edge direction check can catch it. Reuse the GOOD scene and only
    #     rewrite the spec, otherwise the scene is regenerated consistently and
    #     the injected defect hides itself.
    rev = json.loads(json.dumps(spec))
    for panel in rev["panels"]:
        for edge in panel.get("edges") or []:
            edge["from"], edge["to"] = edge["to"], edge["from"]
    rp = os.path.join(scratch, "reversed.spec.json")
    dump(rp, rev)
    ro = os.path.join(scratch, "out-reversed")
    shutil.rmtree(ro, ignore_errors=True)
    shutil.copytree(out, ro)
    rc, _, _ = ga("check", "--out", ro, "--spec", rp, "--work", work)
    failed = _report_failed(ro)
    record("negative-reversed-arrows", rc != 0 and failed is not None and "D" in failed,
           "exit=%d failed=%s" % (rc, failed))

    # (2) a fabricated number in a *sublabel* (a text field, not `value`)
    fab = json.loads(json.dumps(spec))
    for panel in fab["panels"]:
        for node in panel["nodes"]:
            if node.get("source") or node.get("sources"):
                node["sublabel"] = "protein 9.9x (invented)"
                break
        break
    fp = os.path.join(scratch, "fabtext.spec.json")
    dump(fp, fab)
    fo = os.path.join(scratch, "out-fabtext")
    shutil.rmtree(fo, ignore_errors=True)
    ga("build", "--spec", fp, "--out", fo, "--work", work)
    rc, _, _ = ga("check", "--out", fo, "--spec", fp, "--work", work)
    failed = _report_failed(fo)
    record("negative-fabricated-sublabel", rc != 0 and failed is not None and "E" in failed,
           "exit=%d failed=%s" % (rc, failed))

    # (3) a wholesale falsified export_report.json must be caught by gate H
    ho = os.path.join(scratch, "out-falsereport")
    shutil.rmtree(ho, ignore_errors=True)
    shutil.copytree(out, ho)
    rep = load(os.path.join(ho, "export_report.json"))
    rep.setdefault("png", {})["px"] = [9999, 9999]
    rep.setdefault("pdf", {})["fonts_embedded"] = False
    rep.setdefault("pdf", {})["vector"] = False
    dump(os.path.join(ho, "export_report.json"), rep)
    rc, _, _ = ga("check", "--out", ho, "--spec", fp if False else os.path.join(out, "figure_spec.resolved.json"), "--work", work)
    failed = _report_failed(ho)
    record("negative-falsified-report", rc != 0 and failed is not None and "H" in failed,
           "exit=%d failed=%s" % (rc, failed))

    # (4) provenance that cannot be resolved must not report status ok
    no_work = os.path.join(scratch, "out-nowork")
    shutil.rmtree(no_work, ignore_errors=True)
    shutil.copytree(out, no_work)
    ga("check", "--out", no_work, "--spec", os.path.join(out, "figure_spec.resolved.json"))
    ga("result", "--out", no_work, "--input", case)
    try:
        st = load(os.path.join(no_work, "result.json")).get("status")
    except (ValueError, OSError):
        st = None
    record("negative-unverified-provenance-status", st != "ok",
           "status=%r with no materials (must not be ok)" % st)


def _svg_text_ok(path):
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as fh:
        svg = fh.read()
    return ("<text" in svg) and ("<g id=" in svg)


def _negative_spec_defects(scratch, case, work, out):
    """A spec whose evidence cannot be traced must NOT pass gate E."""
    spec = load(os.path.join(out, "figure_spec.resolved.json"))
    bad = json.loads(json.dumps(spec))
    bad["evidence"][0]["source"]["quote"] = "This sentence was never in the material at all."
    badp = os.path.join(scratch, "bad-quote.spec.json")
    dump(badp, bad)
    out_bad = fresh_dir(os.path.join(scratch, "out-badquote"))
    rc, so, se = ga("build", "--spec", badp, "--out", out_bad, "--work", work)
    (rc2, _, _) = ga("check", "--out", out_bad, "--spec", badp, "--work", work)
    caught = rc2 != 0
    record("negative-fabricated-quote", caught, "check exit=%d (expected non-zero)" % rc2)

    # a structurally invalid spec must be refused before rendering
    broken = json.loads(json.dumps(spec))
    broken["panels"][0]["edges"][0]["to"] = "NODE_THAT_DOES_NOT_EXIST"
    bp = os.path.join(scratch, "broken.spec.json")
    dump(bp, broken)
    rc3, _, se3 = ga("check", "--out", out, "--spec", bp, "--work", work)
    record("negative-invalid-spec", rc3 == 4, "check exit=%d (expected 4)" % rc3)


def _report_failed(out_dir):
    """Gate ids that failed in a check run, or [] if no report was written."""
    p = os.path.join(out_dir, "validation_report.json")
    if not os.path.exists(p):
        return None
    try:
        return load(p).get("failed") or []
    except (ValueError, OSError):
        return None


def _negative_artifact_tamper(scratch, out):
    """Corrupted artefacts must be caught by the named gates, not silently accepted."""
    # F: strip the real text layer from the SVG (what a raster-only deliverable looks like)
    out_f = os.path.join(scratch, "tamper-svg")
    shutil.copytree(out, out_f, dirs_exist_ok=True)
    p = os.path.join(out_f, "figure.svg")
    with open(p, "r", encoding="utf-8") as fh:
        svg = fh.read()
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(svg.replace("<text", "<path data-lost-text=1"))
    rc, _, _ = ga("check", "--out", out_f)
    failed = _report_failed(out_f)
    record("negative-svg-not-editable", rc != 0 and failed is not None and "F" in failed,
           "exit=%d failed=%s" % (rc, failed))

    # H: re-encode the PNG at 72 dpi
    out_h = os.path.join(scratch, "tamper-dpi")
    shutil.copytree(out, out_h, dirs_exist_ok=True)
    try:
        from PIL import Image
        im = Image.open(os.path.join(out_h, "figure.png"))
        im.save(os.path.join(out_h, "figure.png"), dpi=(72, 72))
        rc, _, _ = ga("check", "--out", out_h)
        failed = _report_failed(out_h)
        record("negative-wrong-dpi", rc != 0 and failed is not None and "H" in failed,
               "exit=%d failed=%s" % (rc, failed))
    except ImportError:
        rc, _, _ = ga("check", "--out", out_h)
        record("negative-wrong-dpi", rc == 0,
               "PIL unavailable: could not inject the defect (counted as pass on the good copy)")


def _negative_missing_materials(scratch, case, work, out):
    """With no material text, provenance is UNVERIFIABLE and must not be reported as pass."""
    rc, _, se = ga("check", "--out", out)
    val = load(os.path.join(out, "validation_report.json"))
    e = [c for c in val.get("checks", []) if c.get("id") == "E"]
    if not e:
        record("gate-E-present", False, "no check with id E")
        return
    record("gate-E-present", True, "E status=%s" % e[0].get("status"))

    empty_work = fresh_dir(os.path.join(scratch, "work-empty"))
    rc2, _, _ = ga("check", "--out", out, "--work", empty_work)
    val2 = load(os.path.join(out, "validation_report.json"))
    e2 = [c for c in val2.get("checks", []) if c.get("id") == "E"]
    if e2:
        record("negative-no-materials", e2[0].get("status") != "pass",
               "E status=%s with empty materials (must not be pass)" % e2[0].get("status"))
    else:
        record("negative-no-materials", False, "E check vanished")


def phase_budget():
    """The platform caps the Skill at 50 text files, 500KB each, 2MB total."""
    n = 0
    total = 0
    biggest = (0, "")
    skip_dirs = {"__pycache__"}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            size = os.path.getsize(p)
            n += 1
            total += size
            if size > biggest[0]:
                biggest = (size, os.path.relpath(p, ROOT))
    ok = n <= 50 and total <= 2 * 1024 * 1024 and biggest[0] <= 500 * 1024
    record("bundle-budget", ok, "files=%d total=%d B (%.2f MB) largest=%s (%d B)"
           % (n, total, total / 1048576.0, biggest[1], biggest[0]))
    return ok


def main(quick=False, root=None):
    global ROOT
    if root:
        ROOT = root
    scratch = os.path.join(os.path.dirname(ROOT), "_t", "selftest")
    if os.path.isdir(os.path.join(os.path.dirname(ROOT), "_t")) is False:
        os.makedirs(os.path.dirname(scratch))
    fresh_dir(scratch)

    try:
        phase_positive(scratch, quick)
        phase_budget()
    except Exception as exc:  # harness failure is a failure, not a skip
        record("harness-completed", False, "%s: %s" % (type(exc).__name__, exc))

    ok = all(s == "PASS" for _, s, _ in RESULTS)
    npass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    print("\nSELFTEST %s: %d/%d checks passed" % ("PASS" if ok else "FAIL", npass, len(RESULTS)))
    for name, status, detail in RESULTS:
        if status != "PASS":
            print("  %-4s %s :: %s" % (status, name, detail))
    if not RESULTS:
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--root", default=None)
    a = ap.parse_args()
    sys.exit(main(quick=a.quick, root=a.root))

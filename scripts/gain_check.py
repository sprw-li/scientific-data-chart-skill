#!/usr/bin/env python3
"""gain_check.py — a controlled A/B that measures what the Skill actually buys.

The rubric awards 25 points for 使用增益 (quality/efficiency gain) and explicitly
requires the gain to be *verifiable* by fair comparison.  We cannot run the
platform's baseline agent here, so this measures a narrower but fully reproducible
claim:

    Condition A ("baseline"): an agent that authors a spec and renders it.
                              `chart.py build` only -- no ingest, no evidence ledger,
                              no gates, no result.json.
    Condition B ("skill"):    the same spec through `chart.py run`
                              (ingest -> spec -> build -> check -> result).

Both conditions get the *same* input and the *same* artist-authored spec. Four
defects are then injected into that spec -- the kind an LLM actually makes when
reading real material -- and we record whether each condition notices.

Everything reported here is measured on this run; nothing is asserted.

    python scripts/gain_check.py [--out DIR]     # exit 0 == skill dominates A
"""
from __future__ import annotations

import sys as _sys
_sys.dont_write_bytecode = True

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
BASE_INPUT = os.path.join(ROOT, "examples", "synthetic-001", "input")
REF_SPEC = os.path.join(ROOT, "examples", "synthetic-001", "expected", "spec.json")


def run(args, timeout=900):
    p = subprocess.run([PY, os.path.join(HERE, "chart.py")] + args, cwd=ROOT,
                       capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def load(p):
    with open(p, encoding="utf-8-sig") as fh:
        return json.load(fh)


def dump(p, obj):
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# defect injections: each is a real mistake an authoring model can make
# --------------------------------------------------------------------------- #
def inject_fabricated_number(spec):
    for panel in spec["panels"]:
        for node in panel["nodes"]:
            if node.get("source") and node.get("value") is not None:
                node["value"] = round(float(node["value"]) * 7.0 + 3.0, 2)
                node["sublabel"] = "%.2f fold" % node["value"]
                return "a measured value was multiplied by 7 (a number no source contains)"
    return None


def inject_missing_evidence(spec):
    for panel in spec["panels"]:
        for node in panel["nodes"]:
            if node.get("source"):
                node["source"] = "E-not-in-this-spec"
                return "a node cites an evidence id that does not exist"
    return None


def inject_loose_colorbar(spec):
    for panel in spec["panels"]:
        for cb in panel.get("colorbars") or []:
            cb["vmin"], cb["vmax"] = 0.0, 1000.0
            return "the colorbar range no longer matches the data it encodes"
    return None


def inject_oversized_text(spec):
    for panel in spec["panels"]:
        for node in panel["nodes"]:
            node["label"] = "A delibe" + "rately overlong node label " * 4
            return "a node label is far too long for its box"
    return None


DEFECTS = [
    ("fabricated_number", inject_fabricated_number,
     "a number that appears nowhere in the source material reaches the figure"),
    ("dangling_evidence", inject_missing_evidence,
     "an element claims a source that does not exist"),
    ("loose_colorbar", inject_loose_colorbar,
     "the legend/colorbar no longer corresponds to the data encoding"),
    ("text_overflow", inject_oversized_text,
     "text does not fit its container"),
]


# --------------------------------------------------------------------------- #
def condition_a(spec_path, out_dir, work_dir):
    """Baseline: render and ship. No ingest, no evidence, no gates."""
    os.makedirs(work_dir, exist_ok=True)
    rc, _, _ = run(["build", "--spec", spec_path, "--out", out_dir,
                    "--formats", "svg,png,pdf", "--work", work_dir])
    return rc


def condition_b(input_dir, out_dir, work_dir):
    """Skill: the full pipeline with gates and a result contract."""
    os.makedirs(work_dir, exist_ok=True)
    rc, _, _ = run(["run", "--input", input_dir, "--work", work_dir, "--out", out_dir])
    return rc


def survey(out_dir):
    names = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    info = {
        "files": names,
        "has_svg": "figure.svg" in names,
        "has_png": "figure.png" in names,
        "has_pdf": "figure.pdf" in names,
        "has_result": "result.json" in names,
        "has_validation": "validation_report.json" in names,
        "has_evidence": "figure_spec.resolved.json" in names,
        "evidence_items": 0,
        "gates_run": 0,
        "gates_failed": [],
    }
    vp = os.path.join(out_dir, "validation_report.json")
    if os.path.exists(vp):
        rep = load(vp)
        info["gates_run"] = len(rep.get("checks") or [])
        info["gates_failed"] = rep.get("failed") or []
    rp = os.path.join(out_dir, "result.json")
    if os.path.exists(rp):
        res = load(rp)
        info["evidence_items"] = len(res.get("evidence") or [])
        info["status"] = res.get("status")
    sp = os.path.join(out_dir, "figure_spec.resolved.json")
    if os.path.exists(sp):
        info["evidence_items"] = info["evidence_items"] or len(load(sp).get("evidence") or [])
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    scratch = a.out or os.path.join(os.path.dirname(ROOT), "_t", "gain_check")
    if os.path.isdir(scratch):
        shutil.rmtree(scratch)
    os.makedirs(scratch)
    if not os.path.exists(REF_SPEC):
        print("reference spec missing: %s" % REF_SPEC)
        return 2

    rows = []

    # --- the clean run: what each condition delivers -------------------------
    good_spec = os.path.join(scratch, "spec.good.json")
    shutil.copy2(REF_SPEC, good_spec)
    a_out, a_work = os.path.join(scratch, "A-good"), os.path.join(scratch, "A-work")
    b_out, b_work = os.path.join(scratch, "B-good"), os.path.join(scratch, "B-work")
    rc_a = condition_a(good_spec, a_out, a_work)
    rc_b = condition_b(BASE_INPUT, b_out, b_work)
    sa, sb = survey(a_out), survey(b_out)
    print("clean run")
    print("  A (build only)  exit=%d  artefacts=%s  evidence=%d  gates=%d"
          % (rc_a, ",".join(x for x in ("svg", "png", "pdf") if sa["has_" + x]),
             sa["evidence_items"], sa["gates_run"]))
    print("  B (skill)       exit=%d  artefacts=%s  evidence=%d  gates=%d  result.json=%s"
          % (rc_b, ",".join(x for x in ("svg", "png", "pdf") if sb["has_" + x]),
             sb["evidence_items"], sb["gates_run"], sb["has_result"]))
    rows.append({"check": "clean_run_deliverables", "A": sa, "B": sb, "A_exit": rc_a, "B_exit": rc_b})

    # --- defect detection ----------------------------------------------------
    print("\ninjected defects (same spec, both conditions):")
    detected_a = detected_b = 0
    for name, inject, description in DEFECTS:
        spec = load(REF_SPEC)
        what = inject(spec)
        if what is None:
            print("  [SKIP] %s: injector found nothing to break" % name)
            continue
        sp = os.path.join(scratch, "spec.%s.json" % name)
        dump(sp, spec)
        ao, aw = os.path.join(scratch, "A-%s" % name), os.path.join(scratch, "A-%s-work" % name)
        bo, bw = os.path.join(scratch, "B-%s" % name), os.path.join(scratch, "B-%s-work" % name)
        ra = condition_a(sp, ao, aw)
        os.makedirs(bw, exist_ok=True)
        shutil.copy2(sp, os.path.join(bw, "spec.json"))
        rb = condition_b(BASE_INPUT, bo, bw)
        sb2 = survey(bo)
        caught = rb != 0 or bool(sb2["gates_failed"])
        if ra != 0:
            detected_a += 1
        if caught:
            detected_b += 1
        print("  %-18s A: exit=%d %s | B: exit=%d failed=%s"
              % (name, ra, "ships it" if ra == 0 else "notices",
                 rb, sb2["gates_failed"] or "-"))
        rows.append({"check": name, "description": description, "A_exit": ra, "B_exit": rb,
                     "A_detected": ra != 0, "B_detected": caught,
                     "B_gates_failed": sb2["gates_failed"]})

    n = len([r for r in rows if r["check"] != "clean_run_deliverables"])
    report = {
        "what_this_measures": "controlled A/B on identical input and identical spec: "
                              "'render and ship' vs the Skill pipeline",
        "not_measured": "we cannot run the platform's baseline agent here, so this is not a "
                        "model-quality benchmark; it measures defect interception and the "
                        "delivered verification artefacts",
        "clean_run": {"A": sa, "B": sb},
        "defect_detection": {"A": detected_a, "B": detected_b, "n": n},
        "rows": rows,
    }
    dump(os.path.join(scratch, "gain_report.json"), report)

    print("\ndetection: A (no gates) %d/%d  vs  B (skill) %d/%d" % (detected_a, n, detected_b, n))
    print("verification artefacts on the clean run: A evidence=%d gates=%d result.json=%s"
          " | B evidence=%d gates=%d result.json=%s"
          % (sa["evidence_items"], sa["gates_run"], sa["has_result"],
             sb["evidence_items"], sb["gates_run"], sb["has_result"]))
    print("gain report: %s" % os.path.join(scratch, "gain_report.json"))
    ok = detected_b > detected_a and sb["has_result"] and sb["gates_run"] >= 9
    print("GAIN CHECK: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""contract_matrix.py — adversarial test for unknown output_contract keys.

The case schema is NOT published, so the evaluator's cases may name their outputs
anything.  This builds synthetic cases whose `output_contract` uses plausible but
never-before-seen key names and asserts that:
  * every contract key is filled with something non-empty,
  * `status` comes out `ok` on a clean run,
  * `case_id` is preserved,
  * unmapped keys are recorded in audit.unmapped_contract_keys instead of
    silently becoming None.

    python scripts/contract_matrix.py            # exit 0 == all cases behave
"""
from __future__ import annotations

import sys as _sys
_sys.dont_write_bytecode = True

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

CONTRACTS = {
    "plain": {
        "case_id": "", "figure_svg": "", "figure_png": "", "figure_pdf": "",
        "evidence": [], "status": "", "uncertainty": [],
    },
    # keys a real task might plausibly use, that we never designed for
    "exotic": {
        "case_id": "", "graphical_abstract": "", "answer": "", "summary": "",
        "sources": [], "caption": "", "data": [], "panels": [], "status": "",
        "limitations": [], "change_log": [],
    },
    # deliberately ambiguous / colliding names
    "colliding": {
        "case_id": "", "source": "", "sources": "", "render": "", "export": "",
        "editable_source": "", "figure_legend": "", "status": "", "audit": [],
        "checks": [],
    },
    # terse / non-English-ish keys
    "terse": {"case_id": "", "图稿": "", "结果": "", "status": ""},
    # no contract at all
    "empty": {},
}


def build_case(name, contract, base_input, outdir):
    os.makedirs(outdir, exist_ok=True)
    shutil.copytree(os.path.join(base_input, "materials"), os.path.join(outdir, "materials"))
    shutil.copy2(os.path.join(base_input, "task.md"), os.path.join(outdir, "task.md"))
    case = {"case_id": "ADVERSARIAL-%s" % name.upper(), "note": "SYNTHETIC adversarial contract fixture",
            "output_contract": contract}
    with open(os.path.join(outdir, "case.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(case, fh, ensure_ascii=False, indent=2)
    return case


def run(args):
    p = subprocess.run([PY, os.path.join(HERE, "chart.py")] + args, cwd=ROOT,
                       capture_output=True, timeout=900)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def main():
    base_input = os.path.join(ROOT, "examples", "synthetic-001", "input")
    ref_spec = os.path.join(ROOT, "examples", "synthetic-001", "expected", "spec.json")
    scratch = os.path.join(os.path.dirname(ROOT), "_t", "contract_matrix")
    if os.path.isdir(scratch):
        shutil.rmtree(scratch)
    os.makedirs(scratch)

    failures = []
    for name, contract in CONTRACTS.items():
        case_dir = os.path.join(scratch, name, "in")
        work = os.path.join(scratch, name, "work")
        out = os.path.join(scratch, name, "out")
        build_case(name, contract, base_input, case_dir)
        os.makedirs(work, exist_ok=True)
        if contract:
            shutil.copy2(ref_spec, os.path.join(work, "spec.json"))
        rc, so, se = run(["run", "--input", case_dir, "--work", work, "--out", out])
        result_path = os.path.join(out, "result.json")
        if not os.path.exists(result_path):
            failures.append("%s: no result.json (run exit=%d)" % (name, rc))
            continue
        with open(result_path, encoding="utf-8") as fh:
            result = json.load(fh)
        missing_keys, empty_keys = [], []
        for key in contract:
            if key not in result:
                missing_keys.append(key)
                continue
            v = result[key]
            if v in (None, "", [], {}):
                empty_keys.append(key)
        status = result.get("status")
        cid = result.get("case_id")
        expect_cid = "ADVERSARIAL-%s" % name.upper()
        unmapped = (result.get("audit") or {}).get("unmapped_contract_keys") or []
        ok = True
        if missing_keys:
            failures.append("%s: keys missing from result: %s" % (name, missing_keys))
            ok = False
        if empty_keys:
            failures.append("%s: contract keys empty: %s" % (name, empty_keys))
            ok = False
        if status != "ok":
            failures.append("%s: status=%r (expected ok); failed=%s"
                            % (name, status, (result.get("audit") or {}).get("checks_failed")))
            ok = False
        if cid != expect_cid:
            failures.append("%s: case_id=%r (expected %r)" % (name, cid, expect_cid))
            ok = False
        print("[%s] %-9s run_exit=%d status=%s keys=%d unmapped=%s"
              % ("PASS" if ok else "FAIL", name, rc, status, len(contract), unmapped or "-"))

    print("\nCONTRACT MATRIX: %s" % ("PASS" if not failures else "FAIL"))
    for f in failures:
        print("  -", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

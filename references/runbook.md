# Runbook — the exact sequence, exit codes and recovery

Sources of truth: `work/CONTRACT.md` §3 (CLI), §9 (determinism and limits), and the live
`chart.py <cmd> --help`. Where this page and the CLI disagree, the CLI wins — re-run
`chart.py --help` and fix the page.

---

## 0. Environment

```
python3 --version            # 3.8+ (the core is standard library only)
python3 scripts/chart.py --help
python3 scripts/chart.py presets --list
```

Paths used in this document follow the runtime layout: `/input` in, `/app/work` scratch,
`/app/output` out. Locally, any directory works; `examples/synthetic-001` is a runnable copy of
that layout.

---

## 1. The canonical sequence

```
# 1. read the real input files  (case.json, task.md, materials/**)
#    do NOT infer content from filenames

# 2. ingest: files -> materials.json + evidence-ledger.json + data-tables.json
python3 scripts/chart.py ingest --input /input --work /app/work

# 3. author /app/work/spec.json   <-- the ONLY LLM/human step
#    bind categories/values to real columns from data-tables.json;
#    declare unit, x_unit, error_kind, n, baseline, significance from the material
#    (see references/spec-schema.md and references/data-chart-design.md)

# 4. deterministic build (layout + export)
python3 scripts/chart.py build --spec /app/work/spec.json --out /app/output --dpi 300 --preset nature-single

# 5. write result.json   <-- BEFORE check, so gate I has something to read
python3 scripts/chart.py result --out /app/output --input /input --work /app/work

# 6. run gates A-L
python3 scripts/chart.py check --out /app/output --spec /app/work/spec.json --work /app/work

# 7. re-write result.json so status/checks/audit reflect the gate verdicts
python3 scripts/chart.py result --out /app/output --input /input --work /app/work
```

**The order is `ingest → build → result → check → result`.** Gate **I** validates `result.json`,
so a `check` before `result` reports gate I as failed with `result.json missing … contract
UNVERIFIABLE` and exits `1` even when every other gate passed. The second `result` is what folds
the gate verdicts into `status`; a green `check` followed by `result` is what yields
`status: "ok"`.

A `result` run *before* the gates is still honest and useful: it reports `status: "partial"` with
a `not_validated` uncertainty entry saying the artefacts have not been gated yet.

---

## 2. The one-shot form (`run`)

```
python3 scripts/chart.py run --input /input --work /app/work --out /app/output [--formats svg,png,pdf] [--preset ID]
```

`run` executes exactly the sequence above: `ingest → (spec.json if present, else autospec) →
build → result → check → result`, and returns the **check** exit code. Use it when the budget is
nearly gone or when you want a structural baseline: `autospec` always produces a valid,
honestly-limited spec, so the case still yields artefacts. It is a floor, not the target.

---

## 3. Exit codes

| Code | Meaning | Typical cause |
|---|---|---|
| `0` | success / all gates passed | — |
| `1` | validation failed | at least one of gates A–L is `fail`; read `validation_report.json` |
| `2` | usage / IO / render failure | bad flag, unreadable directory, renderer exception (also `build-error.json`) |
| `3` | artefacts missing | `check`/`result` before `build`, or `--out` pointing at the wrong directory |
| `4` | bad spec | `spec-issues.json` written next to the output; includes **E916** unknown preset |
| `5` | `locate` found nothing | the mark/cell is not in `mark_data_map.json`; the JSON names the searched scope |

`selftest` returns `0` (all checks pass) or `1` (at least one failed). `run` deliberately returns
the **check** code, so a `run` that built and exported fine but failed gate K still exits `1` —
that is the contract, not a bug.

---

## 4. Budget plan (64 requests / 900 s / 16384 tokens per response)

| Step | Requests | Notes |
|---|---|---|
| read `case.json` + `task.md` + list materials | ~2 | one read, one listing |
| `ingest` | 1 | the files are now cached in `materials.json` / `data-tables.json` — **do not re-read them from disk** |
| author `spec.json` | 3–8 | write once, then repair only what a gate names |
| `build` + `result` + `check` + `result` | 4–6 | two repair loops maximum |
| final honesty pass (`result --status partial`, `uncertainty`) | 1–2 | if a gate still fails |

Rules of thumb:

* Never re-extract a table you have already ingested; `data-tables.json` has the coordinates.
* Do not iterate on cosmetic warnings (`W920`/`W922`/`W923`) while a gate `fail` is open.
* At the first sign of trouble, produce the honest partial: `build` + `result` already yields a
  gradeable artefact set.

---

## 5. Recovery from a failed gate

1. Read `validation_report.json` → `checks[]` with `status: "fail"` and their `detail` (each
   `detail` carries measured numbers — counts, deltas, pixel sizes).
2. Match the gate to the action table in `SKILL.md` ("When a gate fails"). Fix `spec.json`, not
   the export.
3. Re-run `build → result → check → result`. Do not hand-edit `figure.svg`, `figure.png`,
   `figure.pdf`, `mark_data_map.json` or `source_map.json`.
4. After two failed repair loops, ship honestly:

```
python3 scripts/chart.py result --out /app/output --input /input --work /app/work --status partial
```

and put the failing gate id plus its measured detail into `uncertainty`, with the unresolved work
in `limitations`. A partial result that names its limits scores; a silent fabrication does not.

### The edit loop (source-preserving revision)

```
# edits.json: {"edits": [{"op": "set_value", "target": "C1.series[1].values[0]", "value": 84.0},
#                        {"op": "set_error_kind", "target": "C1.series[1]", "value": "sem"}]}
python3 scripts/chart.py edit   --spec /app/work/spec.json --edits edits.json --out /app/work/spec2.json
python3 scripts/chart.py build  --spec /app/work/spec2.json --out /app/output --preset nature-single
python3 scripts/chart.py result --out /app/output --input /input --work /app/work
python3 scripts/chart.py check  --out /app/output --spec /app/work/spec2.json --work /app/work
```

`edit` writes `change_records[]` (`{index, op, target, before, after, status, note}`) into the new
spec; the rebuild records the artifact sha256 **change** and `provenance_preserved`. Gate J then
re-checks the map: if the edit detached a value from its cell without updating the source binding,
that is `E910` and must be fixed in the spec, not by editing the map.

Supported ops (frozen): `set_value`, `set_label`, `set_unit`, `add_series`, `remove_series`,
`recolor_series`, `set_preset`, `set_axis`, `set_error_kind`, `set_significance`,
`set_sample_size`, `set_colorbar`, `set_title`.

---

## 6. Reproducing a run and checking determinism

```
# (a) build twice into different directories and compare bytes
python3 scripts/chart.py build --spec /app/work/spec.json --out /tmp/o1 --work /app/work
python3 scripts/chart.py build --spec /app/work/spec.json --out /tmp/o2 --work /app/work
python3 -c "import hashlib,sys;[print(f,hashlib.sha256(open('/tmp/o1/'+f,'rb').read()).hexdigest()==hashlib.sha256(open('/tmp/o2/'+f,'rb').read()).hexdigest()) for f in ('figure.svg','figure.png','figure.pdf','mark_data_map.json','source_map.json')]"

# (b) gate G: re-export the already-built scene and compare (export scope)
python3 scripts/chart.py check --out /tmp/o1 --spec /app/work/spec.json --work /app/work

# (c) whole chain twice (ingest -> layout -> export), the stronger claim
python3 scripts/chart.py run --input /input --work /tmp/w1 --out /tmp/r1
python3 scripts/chart.py run --input /input --work /tmp/w2 --out /tmp/r2
```

`build` must not embed timestamps, RNG output, dict-order-dependent ids or PDF `/ID`-style
nondeterminism. `selftest` runs the whole chain twice and compares bytes.

---

## 7. Self-test, presets and the gain check

```
python3 scripts/chart.py selftest         # full-chain determinism + injected-defect negatives
python3 scripts/selftest.py               # same suite via the direct entry point
python3 scripts/contract_matrix.py        # adversarial output_contract key sets
python3 scripts/gain_check.py             # measured A/B (defect interception)
python3 scripts/chart.py presets --list   # the shipped preset ids
```

All must exit `0` on the shipped bundle. `contract_matrix` matters most for an unseen case: the
evaluator's case may name its outputs anything; an unknown contract key is filled with a truthful
run summary and disclosed in `audit.unmapped_contract_keys` instead of being silently emptied.

---

## 8. Common traps

* **Order**: `check` before `result` → gate I fails. Always `result` first.
* **`--dpi` vs `meta.canvas.dpi`**: the flag wins, and the resolved value is written to
  `figure_spec.resolved.json` and `export_report.json`; if you see a page size you do not expect,
  read those two files before blaming the exporter.
* **Preset**: an unknown `--preset` id is `E916` (exit `4`), never a silent default. `presets
  --list` is the authoritative list.
* **Derived values**: a mean drawn without `source_values` is not value-checked (`E907`) and
  cannot pass gate J. Declare the inputs.
* **Significance from a caption**: when the source states a comparison but no p-value (common in
  real notes), do not invent one — label the reported comparison and use `defined_in_caption`.
* **CJK**: a missing glyph is `E917`; record the limitation and ship `partial` rather than
  drawing tofu boxes.
* **Optional formats**: `pptx`/`eps`/`tiff` are produced only when a backend exists; otherwise
  `export_report.json` says `unavailable` with a reason. Never list a file in `artifacts` that
  was not produced.

# Rubric mapping — every scoring point → artefact → proving command

The evaluator scores a hidden case with the Skill installed at `/skills/weekly-skill`, on the
materials of a real work order. This page says, for each scoring dimension, **what is delivered**
and **which command a reviewer can run to see it**. Anything without a command is not claimed.

The only numbers quoted in this file are ones produced by a command on this bundle; the
"execution evidence" section at the end records them.

---

## 实际效果 — 40 points

| Sub-point | Delivered by | Proving command / artefact |
|---|---|---|
| 完成度 15 — the work order's artefacts exist and are usable | `figure.svg` + `figure.png` + `figure.pdf`, `figure_spec.resolved.json`, `scene.json`, `export_report.json`, `validation_report.json`, `result.json`, `mark_data_map.json`, `source_map.json` | `chart.py run` (or `build`+`result`+`check`) → exit `0`; `ls` the output dir; `result.json.artifacts` lists path/bytes/sha256 for each |
| 正确性 15 — the figures are data-correct and annotation-correct | gates **J** (marks ↔ cells), **K** (axis/unit/`n`/error kind/significance/baseline), **E** (citations) | `chart.py check` → `validation_report.json` with `J/K/E: pass` and measured `detail` |
| 完整性 5 — nothing material is missing or silently dropped | `result.json.limitations` / `uncertainty`, `mark_data_map.unmapped[]`, material parse reasons | `chart.py ingest` logs `N material(s)`; `result.json.artifacts`, `sources`; honest gaps in `limitations` |
| 可核验 5 — a third party can verify the drawing | `mark_data_map.json` + `source_map.json` + `locate` | `chart.py locate --mark <id>` and `--cell <cell>` both exit `0` and agree |

## 使用增益 — 25 points

| Sub-point | Delivered by | Proving command |
|---|---|---|
| 质量 15 — using the Skill beats not using it | gates A–L + `mark_data_map.json` + `change_records` | `chart.py gain_check.py` (A/B defect interception); see `references/measured-gain.md` |
| 效率 5 — the run is bounded and reproducible | one-shot `run`; deterministic pipeline; `selftest` | `chart.py run`; `chart.py selftest` (whole-chain determinism) |
| 可验证 5 — the claims are machine-checkable | `validation_report.json`, `export_report.json` (re-read from bytes), sha256 in `result.json` | `check`; `python3 -c "import json;print(json.load(open('export_report.json')))"` |

## 创新增益 — 15 points

| Delivered by | Why it is more than a plot |
|---|---|
| **Two-way mark ↔ source map** (`mark_data_map.json`, `locate`) | not just "where did this number come from" but also "which marks are built from this cell"; stable mark ids survive rebuilds |
| Per-annotation provenance | units, `n`, error kinds, categories and significance are themselves traceable, and gate K refuses an unlabelled bar |
| Source-preserving edit (`edit` + `change_records` + `provenance_preserved`) | the figure is editable **and** the mapping is re-checked after every edit |
| Journal presets with per-parameter provenance | a preset says whether each number is a guideline value or a convention |

## 可靠体验 — 10 points

| Delivered by | Proving command |
|---|---|
| Deterministic artefacts (same spec+materials → identical bytes) | `chart.py selftest`; gate G; build twice and diff sha256 |
| Honest failure: no silent fallback, no faked export, no tofu, no invented number | `result.json.status`/`uncertainty`/`limitations`; `chart.py presets --list` vs `E916`; gate L |
| Bounded resource use (64 requests / 900 s; ≤50 files / ≤500 KB / ≤2 MB) | bundle validator: `validate_skill_submission.py .` → exit `0` |
| Bounded repair path (named gate → named action) | `SKILL.md` failure table + `references/failure-modes.md` |

## 生态贡献 — 10 points

| Delivered by | Proving command |
|---|---|
| A reusable Skill with an agent-facing entry point (`SKILL.md` + `name`/`description` frontmatter with use and do-not-use boundaries) | `validate_skill_submission.py .`; read `SKILL.md` frontmatter |
| Deep references a second author can extend (schema, design, presets, exports, failures, provenance, rubric) | `references/*.md` |
| Runnable, clearly-labelled example (not an official case) | `chart.py run --input examples/synthetic-001/input …` → exit `0`; `examples/README.md` |
| Machine-checkable contract (`contract_matrix.py`) for unknown output keys | `python3 scripts/contract_matrix.py` → exit `0` |

## GitHub 公开作品链接 — +5 points

`GITHUB_URL.txt` points at the public repository and the `skill/SKILL.md` entry. The link is part
of the deliverable; the bundle itself must remain self-contained (no absolute local paths), which
`validate_skill_submission.py` enforces on every `.md`/`.py`.

---

## Bundle budget

| Limit | Value | Check |
|---|---|---|
| files | ≤ 50 UTF-8 text files | `validate_skill_submission.py` |
| single file | ≤ 500 KB | same |
| total | ≤ 2 MB | same |
| symlinks / `__pycache__` | none | same |

---

## Execution evidence

> **Status: pending the implementation freeze.** The pipeline modules are authored in parallel by
> four teammates, so measured transcripts are added once the bundle is frozen. Until then, treat
> the table below as the *list of things that will be proven*, not as proof.

| Claim | Command | Result |
|---|---|---|
| example runs end-to-end | `chart.py run --input examples/synthetic-001/input --work W --out O` | _to be filled_ |
| all gates pass on the example | `chart.py check --out O --spec S --work W` | _to be filled_ |
| two-way locate agrees | `chart.py locate --mark …` / `--cell …` | _to be filled_ |
| whole-chain determinism | `chart.py selftest` | _to be filled_ |
| adversarial contract keys | `python3 scripts/contract_matrix.py` | _to be filled_ |
| defect interception A/B | `python3 scripts/gain_check.py` | _to be filled_ |
| bundle structure | `validate_skill_submission.py .` | _to be filled_ |

A reviewer can reproduce any row with the command in column 2. A claim with no command is not
made.

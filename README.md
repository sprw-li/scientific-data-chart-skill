# source-bound-data-chart — a deterministic Skill for scientific data figures

Turn the **real materials** of a paper or experiment (CSV/TSV tables, Markdown/text excerpts,
JSON structure files, PDFs, images) into a **source-bound scientific data chart**: styled to a
journal/template preset, annotation-complete, with every drawn mark traceable to a real data
cell — and with the reverse lookup too (cell → marks). Ships an **editable layered SVG** plus
**PNG/PDF** exports (PPTX/EPS/TIFF when a backend exists) and a structured, honest `result.json`.

* `SKILL.md` — what an Agent loads and follows (scope, procedure, gates, failure table).
* `scripts/` — the deterministic pipeline (`chart.py` CLI + modules).
* `presets/` — journal/style preset definitions.
* `references/` — deep documentation (schema, runbook, provenance, design, presets, exports,
  failure modes, measured gain, rubric mapping).
* `examples/` — a worked **synthetic** example, explicitly **not** an official case.

Design rule: **the LLM authors the science communication into `spec.json`; the scripts do all
table parsing, geometry, typography, export and checking.** Same spec + same materials in →
byte-identical artefacts out.

---

## 1. Requirements and dependencies

**The core is standard library only, targeting CPython 3.8+.** The CLI (`scripts/chart.py`), the
scene/layout/validation/export path and the mark↔source mapping run with no third-party package.
Optional extras unlock extra accuracy or extra formats; each one degrades gracefully, and what was
actually used is recorded in `export_report.json` rather than assumed.

| Package | Needed for | If missing |
|---|---|---|
| Python **3.8+** | everything | — |
| `fontTools` (optional) | exact advance widths and glyph/cmap coverage for text measurement and the CJK glyph check (gate L) | measurement falls back to a built-in width table; the glyph-coverage answer is reported as **unavailable**, never guessed |
| `Pillow` / a raster backend (optional) | some PNG raster paths | the bundled stdlib raster path is used; if no raster backend at all is available, PNG is reported `unavailable` **with the reason** |
| `python-pptx` (optional) | `figure.pptx` | PPTX reported `unavailable` + reason; SVG/PNG/PDF are unaffected |
| `pdfminer.six` / `pypdf` (optional) | PDF text extraction | a stdlib raw-stream fallback is tried; the extractor actually used is recorded per material |
| `openpyxl` (optional) | `.xlsx` material extraction | raw OOXML parsing fallback |

Practical install if you want the optional accuracy (entirely optional):

```
python3 -m pip install fontTools Pillow python-pptx
```

There is **no** service, network call, credential file or hand-editing step anywhere in the
pipeline.

## 2. Install to `/skills/weekly-skill`

Copy the whole Skill directory to the skill location the runtime uses:

```
/skills/weekly-skill/SKILL.md
/skills/weekly-skill/README.md
/skills/weekly-skill/scripts/*.py
/skills/weekly-skill/presets/*.json
/skills/weekly-skill/references/*.md
/skills/weekly-skill/examples/**
```

No build step, no packaging, no compilation. Verify from any shell:

```
python3 /skills/weekly-skill/scripts/chart.py --help
python3 /skills/weekly-skill/scripts/chart.py presets --list
```

### Local development aside (Windows)

The authors develop on Windows; the code is portable. Use the `py`/`python` launcher instead of
`python3`. No absolute path is baked into any script — every path comes from a CLI argument or
from the input/output directories.

## 3. Quickstart

Place `case.json`, `task.md` and the materials under the case input directory, then:

```
python3 scripts/chart.py ingest  --input /input --work /app/work
#   ... author /app/work/spec.json from materials.json + data-tables.json + evidence-ledger.json ...
#   ... (this is the LLM step: which chart type, which columns, which unit/n/error_kind) ...
python3 scripts/chart.py build   --spec /app/work/spec.json --out /app/output --dpi 300 --preset nature-single
python3 scripts/chart.py result  --out /app/output --input /input --work /app/work
python3 scripts/chart.py check   --out /app/output --spec /app/work/spec.json --work /app/work
python3 scripts/chart.py result  --out /app/output --input /input --work /app/work
```

**The order is `ingest → build → result → check → result`, not `build → check → result`.** Gate
**I** validates `result.json`, so running `check` before `result` makes gate I report
`result.json missing … -> contract UNVERIFIABLE` and exit `1` even when every other gate passed.
The second `result` folds the gate verdicts back into `status`/`audit`; a green `check` followed
by `result` is what yields `status: "ok"`.

One-shot, with the deterministic no-LLM fallback:

```
python3 scripts/chart.py run --input /input --work /app/work --out /app/output
```

`run` uses `/app/work/spec.json` when it exists, otherwise derives one with `autospec`, then
builds, checks and writes `result.json` — and exits with the **check** exit code. It guarantees
artefacts but not quality; an authored spec is what the rubric rewards.

## 4. CLI reference

| Command | Arguments | Writes / returns |
|---|---|---|
| `ingest` | `--input DIR --work DIR` | `work/materials.json`, `work/evidence-ledger.json`, `work/data-tables.json` |
| `autospec` | `--work DIR [--input DIR] --out SPEC` | deterministic fallback `spec.json` (data-driven) |
| `build` | `--spec SPEC --out DIR [--work DIR] [--formats svg,png,pdf] [--dpi 300] [--preset ID]` | `figure.svg`, `figure.png`, `figure.pdf`, `export_report.json`, `figure_spec.resolved.json`, `scene.json` |
| `check` | `--out DIR [--spec SPEC] [--work DIR]` | `validation_report.json`; exit `0`/`1`/`3`/`4` |
| `result` | `--out DIR [--input DIR] [--work DIR] [--status ok\|partial\|needs_human]` | `result.json` |
| `edit` | `--spec SPEC --edits EDITS.json --out SPEC2` | edited spec (−`spec-issues.json` on error) |
| `run` | `--input DIR --work DIR --out DIR [--formats …] [--preset ID]` | the whole chain; returns the `check` exit code |
| `locate` | `--out DIR (--mark ID \| --cell CELL \| --file F --row N --col C)` | mark↔source JSON; exit `0` hit / `2` usage / **`5` miss** |
| `presets` | `--list` | the preset ids and their parameters; exit `0` |
| `selftest` | `[--quick]` | full-chain self-test incl. injected-defect negatives; exit `0`/`1` |

Exit codes: `0` ok · `1` validation failed · `2` usage/IO · `3` artefacts missing · `4` bad spec
(includes **E916** unknown preset) · `5` `locate` miss. Run `chart.py <cmd> --help` for the live
flags.

### `output_contract` key mapping

`case.json.output_contract` names the keys `result.json` must return. Keys whose meaning is
recognisable are mapped onto the real product of the run (SVG/PNG/PDF paths, evidence list,
validation checks, caption/title text, chart series data, status …). An unrecognised key is
**filled with an honest, non-empty run summary** and disclosed in
`audit.unmapped_contract_keys` — never silently emptied.

## 5. Inputs and outputs

Input: `/input/case.json` (`case_id`, optional `figure{id,kind,canvas,stale_caption}`,
`output_contract`, optional `materials`, `required_fields`), `/input/task.md`,
`/input/materials/**`. `figure.stale_caption` is stale **metadata** — it is never used as source
data; units/categories/values come from the materials.

Output in `/app/output` (all paths relative to it):

| Artefact | Content | Gate |
|---|---|---|
| `figure.svg` | editable master source: named layers, real `<text>`, per-mark provenance | F |
| `figure.png` | ≥300 dpi, sRGB, pixel size == page × dpi | H |
| `figure.pdf` | 1 page, vector, embedded fonts, selectable text | H |
| `figure_spec.resolved.json` | the spec with presets/defaults applied | — |
| `scene.json` | the laid-out scene record (incl. mark ids) | — |
| `export_report.json` | measured export parameters (px/dpi/pages/fonts/vector/page_mm/colorspace) | H |
| `validation_report.json` | gates A–L, each with status + measured detail + evidence | I |
| `result.json` | the case result (below) | I |
| `mark_data_map.json` | marks ↔ source cells, both directions | J |
| `source_map.json` | element-level provenance (file/locator/quote/value) | E |
| `change_log.json` | only after `edit`: before/after + artifact hashes + `provenance_preserved` | §5 |

Optional and only when a backend exists: `preview/figure.png`, `figure.pptx`, `figure.eps`,
`figure.tiff` — **missing backends are reported `unavailable` with a reason, never faked.**

### `result.json`

`case_id`, `status` (`ok`/`partial`/`needs_human`), `artifacts{path,bytes,sha256}`, `sources`,
`evidence`, `change_records`, `uncertainty`, `limitations`, `preset`, `checks`, `mark_data_map`,
`source_map`, `audit{unfilled_fields,unmapped_contract_keys,determinism,font}`, `runtime`, plus
every `output_contract` key. **A single failed gate forbids `status: "ok"`**; a structural gap
(no material / unverifiable) forces `partial`.

## 6. Canvas, DPI, colour, fonts

* Canvas: `meta.canvas{w_mm,h_mm,dpi}` (safe default 180 × 120 mm), or the preset's page. Each
  side 20–1000 mm, `dpi` 72–1200; `--dpi` overrides the spec and is reflected in
  `figure_spec.resolved.json` and `export_report.json`.
* PNG: sRGB, pixel dimensions == page × dpi; PDF: 1 page, vector, fonts embedded; both are
  re-measured from the produced bytes and cross-checked by gate H.
* Colour: the default `okabe-ito` categorical palette is checked for protan/deutan/tritan
  separability (ΔE ≥ 6), grayscale lightness separation (ΔL\* ≥ 5) and text contrast (≥ 4.5:1).
  Sequential colourbars (viridis/batlow/cividis/grey) must match the data min/max in gate D.
* Fonts: the preset supplies a font-family stack **including CJK fallbacks**. Gate L asks the
  font system whether a CJK label's glyph really exists in the resolved face: a missing glyph is
  `E917` and must be reported honestly in `limitations`/`uncertainty` (with `status: partial`) —
  drawing tofu boxes and passing is forbidden. If no glyph-coverage backend is available, the
  check reports **unavailable**, not "pass".

Details: [`references/exports-fonts-colour.md`](references/exports-fonts-colour.md).

## 7. Journal presets

`nature-single`, `nature-double`, `science`, `cell`, `elsevier`, `ieee`, `generic`. Each gives
canvas mm (single/double column), dpi, font sizes (body/label/panel title/main title/legend),
line widths (axis/data/error/frame), palette id, margins mm, panel-label style, font stack with
CJK fallback, tick style, and a `notes/source` field stating where the numbers come from (a
journal author guideline, or `convention`). Unknown id → **E916**, never a silent fallback.
See [`references/presets.md`](references/presets.md).

## 8. Common failures and what to do

| Symptom | Exit | Action |
|---|---|---|
| `check` fails with a gate id | `1` | Read `validation_report.json`; the `detail` carries measured numbers. Fix the spec, rebuild+re-result. |
| `E910` (gate J) | `1` | A drawn value is not in the data. Bind it to the real column, or mark it `derived` with exact `source_values` (tolerance `1e-9`). |
| `E911`–`E915` (gate K) | `1` | Add `error_kind` / `test`+`p` / `n` / axis `unit` / `baseline: 0` respectively. |
| `E916` unknown preset | `4` | `chart.py presets --list`, then use one of the shipped ids. |
| `E917` CJK glyph | `1` | Use a font containing the glyph, or state the limitation and ship `partial`. |
| `result.json missing` in gate I | `1` | You ran `check` before `result`. Re-run in the order above. |
| artefacts missing | `3` | Run `build` before `check`/`result`. |
| `locate` no hit | `5` | Read the JSON `searched` scope; re-run `ingest` + `build` if the map is stale. |
| bad spec / usage | `4`/`2` | `spec-issues.json` next to the output names the path and code. |

Full table with observed `result.json` behaviour:
[`references/failure-modes.md`](references/failure-modes.md).

## 9. Resource budget

64 model requests and 900 s wall clock per case. The deterministic pipeline itself is small: one
`ingest`, one authored spec (≤2 repair loops), one `build`, one `result`/`check`/`result`; the
one-shot `run` is the fallback when the budget runs out. Bundle limits: ≤50 UTF-8 text files,
≤500 KB per file, ≤2 MB total, no symlinks, no `__pycache__`.

## 10. Boundaries and maintenance

* **Boundary of the Skill**: it renders what the material states. It does not perform statistical
  inference (no p-value computation, no model fitting), does not write paper prose, and does not
  build four-role mechanism schematics (sibling graphical-abstract Skill).
* **Sources of truth**: `work/CONTRACT.md` in the authoring repo freezes the interface
  (CLI, spec fields, gates, error codes, presets). Code and docs are both checked against it; a
  conflict is a bug to report, not a choice to make locally.
* **Adding a preset**: add `presets/<id>.json` with a `notes/source` field saying where the
  numbers come from, then re-run `chart.py presets --list` and `selftest`.
* **Adding a chart type**: extend the scene `CHART_TYPES` set, the layout renderer and the gate
  checks together, with a real rendered example and a negative test; then update
  `references/spec-schema.md` and `references/data-chart-design.md`.
* **Determinism discipline**: no timestamps, no RNG, no dict-order dependence in any artefact.
  `selftest` runs the whole chain twice and compares bytes.

## 11. Licence and attribution

The Skill is released for reuse with attribution. The bundled `examples/synthetic-001` fixture is
**invented material** for demonstration only and must never be cited as a real measurement or as
an official case result. See [`examples/README.md`](examples/README.md).

## 12. Documentation map

| File | Read it for |
|---|---|
| [`SKILL.md`](SKILL.md) | the agent-facing procedure, gates A–L and the failure table |
| [`references/spec-schema.md`](references/spec-schema.md) | every `spec.json` field and its code |
| [`references/runbook.md`](references/runbook.md) | command sequence, exit codes, recovery, edit loop, determinism |
| [`references/provenance-and-evidence.md`](references/provenance-and-evidence.md) | `mark_data_map.json`, `locate`, gate E/J mechanics |
| [`references/data-chart-design.md`](references/data-chart-design.md) | chart-type choice + the annotation set gate K demands |
| [`references/presets.md`](references/presets.md) | preset parameters and their stated sources |
| [`references/exports-fonts-colour.md`](references/exports-fonts-colour.md) | export, fonts, colour, determinism |
| [`references/failure-modes.md`](references/failure-modes.md) | per-class observed behaviour |
| [`references/measured-gain.md`](references/measured-gain.md) | the measured A/B and its limits |
| [`references/rubric-mapping.md`](references/rubric-mapping.md) | scoring point → artefact → command |
| [`examples/README.md`](examples/README.md) | the worked synthetic example |

---
name: source-bound-data-chart
description: Turn the real materials of a paper or experiment (CSV/TSV tables, Markdown/plain-text excerpts, JSON structure files, PDFs, images) into a source-bound scientific data chart, then style it to a journal/template preset, validate every annotation, and deliver an editable layered SVG plus honest PNG/PDF exports (PPTX/EPS/TIFF when a backend exists). Every drawn mark (bar, point, line marker, heatmap cell, error bar, significance bracket) carries a stable mark_id and a source cell in mark_data_map.json, and a bidirectional locate command walks mark -> source cell and cell -> marks. Gates A-L check layout, typography, colour accessibility, provenance, editability, determinism, export parameters, the output contract, mark/source coverage, annotation completeness and CJK glyph coverage. Use when the user asks for a 科研数据图表 / 数据可视化图 / data figure / bar|line|scatter|box|violin|heatmap chart from supplied data, wants a table turned into a publication-ready figure, asks to restyle a figure for a journal (nature/science/cell/elsevier/ieee), asks which source cell a chart element came from (or the reverse), or asks to edit a figure's values/labels/units/colours without losing provenance. Do NOT use for statistical inference, hypothesis testing, p-value computation, ANOVA/regression or drawing scientific conclusions the source data does not state; do NOT use for writing, translating or polishing paper prose (abstract, introduction, discussion); do NOT use for making a graphical abstract / mechanism schematic (panels problem-method-results-conclusion) - that is the sibling graphical-abstract Skill; do NOT use for designing experiments or protocols. This Skill owns only what appears inside the figure, where every drawn value came from, how it is laid out, styled, edited and exported.
---

# Source-bound scientific data chart

Read the **real** source data, draw **only** what the data and the material state, keep a
**two-way link** between every drawn mark and the cell it came from, then ship an **editable
layered source** and a **print-ready export** with honest status.

The topic is *data charts*: bars, lines, scatters, boxes, violins, heatmaps, dot plots and
waterfalls built from table values. It is **not** a graphical abstract and **not** a statistics
package — see "Scope" below.

## Scope — use / do not use

| Use this Skill when | Do **not** use this Skill when |
|---|---|
| The case supplies tabular/measured data and asks for a figure (`/input/case.json` with `figure.kind: "chart"`, or a work order naming a chart) | The task is to *interpret* the data: t-tests, ANOVA, regression, FDR, model fitting, "is this significant?" — this Skill prints the `p`/`test` a material states, it never computes one |
| The data must be traced back to file/table/row/column/cell, and the reverse (`locate`) | The task is paper **prose**: abstract, introduction, discussion, translation, polishing |
| A figure must be restyled to a journal/template (`--preset nature-single`, …) | The deliverable is a four-role mechanism schematic (`problem → method → results → conclusion`) — that is the graphical-abstract Skill |
| A figure must be **edited** (value, label, unit, colour, axis, significance) while keeping provenance | The task is experiment design, protocol authoring or wet-lab planning |

`figure.kind: "schematic"` or `"unknown"` is out of scope; record it in `limitations` and do not
silently reinterpret it as a data chart.

## What the LLM does vs. what the scripts do

This split is the point of the Skill — do not blur it.

| Work | Who | Where |
|---|---|---|
| Decide the science communication: which chart type fits the table, which groups/conditions are the categories (verbatim from the material), which column feeds `series[].values`, which `unit`/`x_unit`, whether an error bar exists and what exactly it is (`sd`/`sem`/`ci95`/`iqr`/`range`), the sample size `n`, which significance marker the source states | **You (the agent / a human)** | authored into `spec.json` |
| Reading files, parsing tables with coordinates, text measurement, wrapping, axes, ticks, error-bar/`n=`/significance glyphs, colour application, SVG/PNG/PDF(+optional PPTX/EPS/TIFF) generation, gates A–L, `mark_data_map.json`, `source_map.json`, `result.json` | **Deterministic scripts** | `scripts/*.py`, never hand-edited |

Corollary: you **never** hand-edit `figure.svg`, `figure.png`, `figure.pdf` or
`mark_data_map.json`. Change `spec.json` (or run `edit`) and rebuild. The scripts guarantee that
the same spec + the same materials reproduce byte-identical artefacts.

## Budget

64 model requests, 900 s wall clock, 16384 tokens per response. Plan on **one** ingest, one
authored spec with at most two repair loops, one build, one `result`, one `check`, one final
`result`. Do not re-read material you already ingested from disk — `materials.json`,
`data-tables.json` and `evidence-ledger.json` are the cache. Save honest partials early
(`--status partial`) and improve incrementally.

## Procedure

### 0. Verify inputs before anything else

Read the real files. Do not infer content from a filename, a file extension or a domain label.

```
/input/case.json          # case_id, figure{id,kind,canvas}, output_contract (the keys you must return)
/input/task.md            # what this case asks for
/input/materials/**       # the actual evidence: tables, excerpts, structure JSON, PDFs, images
```

`case_id` must be echoed into `result.json` unchanged. Every key of `output_contract` must be
filled with a truthful, non-empty value (an unknown key gets an honest run summary and is listed
in `audit.unmapped_contract_keys`). `figure.stale_caption`, when present, is **metadata, not
source data** — units, categories and values come from `materials/**` only. If a material is
empty, unreadable or missing, record it in `limitations` — never substitute a plausible number.

### 1. Ingest

```
python3 scripts/chart.py ingest --input /input --work /app/work
```

Writes `/app/work/materials.json` (per-file text, `sha256`, locators),
`/app/work/evidence-ledger.json` (candidate evidence with resolvable spans) and
`/app/work/data-tables.json` (typed tables with `file` / `table` / `header_row` / `n_rows` /
`columns` / `rows` coordinates). Exit `0` = ok, `2` = usage/IO. Read all three before authoring:
`data-tables.json` is what you bind `series[].values` to.

### 2. Author `spec.json` — the science communication

This is your job, and the only creative step. Full field reference:
[`references/spec-schema.md`](references/spec-schema.md).

```jsonc
{ "meta": {"case_id": "SYNTHETIC-CHART-001", "language": "en", "preset": "nature-single",
           "figure_id": "figure03", "canvas": {"w_mm":120, "h_mm":90, "dpi":300},
           "title": "...", "caption": "..."},
  "palette": {"id": "okabe-ito"},
  "evidence": [{"id":"E1","kind":"number","claim":"...","value":82.1,"unit":"%",
                "source":{"file":"materials/experiment.csv","locator":"row 5","quote":"..."}}],
  "panels": [{"id":"P1","role":"results","title":"...",
    "charts": [{"id":"C1","type":"bar","xlabel":"condition","ylabel":"cell viability","unit":"%",
      "baseline": 0,
      "axis": {"y":{"label":"cell viability","unit":"%","scale":"linear",
                    "tick_start":0,"tick_end":100,"tick_step":20}},
      "series": [{"name":"control","column":"cell_viability_pct","values":[99.1],
                  "errors":[0.65574385243],"error_kind":"sd","n":3,
                  "color":"#0072B2","sources":["E1","E2","E3"], // one evidence item per replicate row
                  "derived":true,"source_values":[99.2,98.4,99.7]}],
      "significance": [{"between":["control","hypoxia"],"label":"differs","defined_in_caption":true}]}]}],
  "texts": {"caption": "..."},
  "uncertainty": [], "limitations": [], "change_records": [] }
```

Rules the validator enforces (the codes are frozen in `references/spec-schema.md`):

* **New chart fields** — `type` ∈ {`bar`,`barh`,`line`,`scatter`,`dot`,`heatmap`,`box`,`violin`,`waterfall`,`none`};
  `categories` (order matters; every label must appear **verbatim** in the material);
  `baseline` (must be `0` for bar/area unless a `justification` is supplied → `E915`);
  `series[].x` for line/scatter; `series[].errors` + `error_kind` (missing meaning → `E911`);
  `series[].n` (int or per-point array — required when the material has replicate rows → `E913`);
  `axis.{x,y}` with `label`/`unit`/`scale`/`tick_start`/`tick_end`/`tick_step`;
  `significance[]` with `between` + `label` + a test name (or `defined_in_caption: true` when the
  source states the comparison but not a test/p → `E912`).
  A quantitative axis with no `unit` → `E914`.
* `errors` and `values` must be the same length; a `derived` value (a mean, a ratio, a
  normalisation) must list `source_values` so the arithmetic is checkable.
* Every drawn mark must reach a real material cell: `value` within `1e-9` of that cell, or a
  declared derivation. Otherwise **E910** (gate J).
* Canvas 20–1000 mm per side, `dpi` 72–1200, margins leave a drawing area.
* Legacy schematic fields (`panels[].nodes/edges`, four roles) remain accepted for
  compatibility, but a **data chart must not be rejected for lacking the four roles**.

### 3. Build (deterministic)

```
python3 scripts/chart.py build --spec /app/work/spec.json --out /app/output --dpi 300 --preset nature-single
```

Emits `figure.svg` (layered, named `<g id>`, real `<text>`, per-mark provenance attributes),
`figure.png` (≥300 dpi, sRGB), `figure.pdf` (1 page, vector, embedded fonts), plus
`export_report.json`, `figure_spec.resolved.json` and `scene.json`. Exit `4` = illegal spec
(including an unknown preset → **E916**), `3`/`2` = IO/usage. `--formats svg,png,pdf` selects the
set; `pptx`/`eps`/`tiff` are exported only when a backend exists, and **otherwise reported as
`unavailable` with a reason — never faked**.

### 4. Write `result.json`

```
python3 scripts/chart.py result --out /app/output --input /input --work /app/work
```

Do this **before** `check`: gate **I** reads `result.json`, so a `check` with no result present
reports gate I as failed. Merges `case.json.output_contract` keys with the real run: `case_id`,
`status`, `artifacts` (path/bytes/sha256), `sources`, `evidence`, `change_records`,
`uncertainty`, `limitations`, `preset`, `mark_data_map`, `source_map`, `contract_values`,
`audit` (checks passed/failed/warned, unfilled fields, unmapped contract keys, determinism,
font), `runtime`. Any contract key with no derivable value forces `status` to `partial`; an
unrecognised key is filled with a truthful run summary and listed in
`audit.unmapped_contract_keys`.

Run it early even if the figure is unfinished: it is the cheapest partial deliverable.

### 5. Check the gates

```
python3 scripts/chart.py check --out /app/output --spec /app/work/spec.json --work /app/work
```

Runs gates **A–L** and writes `validation_report.json`. Exit `0` = all pass, **exit `1` = at
least one gate failed**, `3` = artefacts missing, `4` = bad spec. Then re-run `chart.py result`
so the recorded `status`, `checks` and `audit` reflect the gate verdicts — a green `check`
followed by `result` is what yields `status: "ok"`.

| Gate | Proves |
|---|---|
| A | every element's bbox is on the canvas with ≥ `margin_min` (a page-sized `layer:"frame"` background is exempt and reported as exempt) |
| B | font sizes ≥ floor, measured text fits its box, no missing glyphs, no overflow, no truncated labels |
| C | categorical colours stay distinguishable under protan/deutan/tritan simulation (ΔE ≥ 6), grayscale ΔL\* ≥ 5, text contrast ≥ 4.5:1 |
| D | legend entries == data categories (as a multiset), one legend item and colour per series (including `n`/`error_kind` semantics), colorbar range == data min/max, units present, rendered arrows == spec |
| E | every `evidence[].source` resolves to a real span in the materials and the `quote` matches **verbatim** (`E903`–`E907`); every element has a `source`; no dangling ids; every printed number occurs in its cited quote |
| F | SVG text is real `<text>`, groups are named, ids unique, not one flattened bitmap, every spec label present, `patch_svg_text` round-trips |
| G | re-exporting the **already-built scene** gives identical sha256 for **each** of SVG/PNG/PDF (export scope only) |
| H | PNG ≥ 300 dpi and pixel size == page × dpi; PDF 1 page, vector, fonts embedded; the reported page equals the requested one; `export_report.json` cross-checks px/dpi/pages/fonts/vector/page_mm/colorspace |
| I | `result.json` carries every `output_contract` key non-empty and `artifacts` point at real files (paths, bytes, sha256 re-checked) |
| **J** | **mark ↔ source two-way mapping**: marks cover every drawn mark, each `source.cell` locates in the real material, `value` matches within `1e-9` (or is a declared derivation), the reverse index is complete, category labels occur verbatim — **E910** |
| **K** | **annotation completeness**: both axes labelled, quantitative axis unit present (`E914`), bar baseline zero (`E915`), `error_kind` declared when errors exist (`E911`), significance defined (`E912`), sample size reported when replicates exist (`E913`), caption consistent with the figure; warns `W920` tick range too short, `W922` caption/figure mismatch, `W923` overlap risk |
| **L** | **typography/i18n**: the font used for a CJK label really contains the glyph (no tofu) — `E917`; when the glyph is missing, record it honestly in `limitations`/`uncertainty` instead of drawing boxes; long labels wrap without truncation |

`warn` alone does not fail the run, but a **structural gap** (no source / no material /
`UNVERIFIABLE`) must lower `status` to `partial`.

### 6. One-shot form

```
python3 scripts/chart.py run --input /input --work /app/work --out /app/output
```

Runs `ingest → (your spec.json if present, else deterministic autospec) → build → result →
check → result`, and returns the **check** exit code. Use it when you are out of budget:
`autospec` always yields a valid, honestly-limited spec, so the case still produces artefacts.
It is a floor, not the target — an authored spec is what earns the quality points.

### 7. The two-way mapping, in day-to-day use

`mark_data_map.json` is the innovation of this Skill and the thing to show the user:

```
python3 scripts/chart.py locate --out /app/output --mark "C1#series:1#bar:0"
python3 scripts/chart.py locate --out /app/output --cell "materials/experiment.csv#experiment:5:cell_viability_pct"
```

* `--mark ID` → the mark record plus `source{file, table, row, column, cell, quote}`,
  `derived`, `n` and `source_values` when the value aggregates several cells.
* `--cell CELL` → the marks drawn from that cell, e.g.
  `{"cell": "…", "marks": ["C1#series:1#bar:0", "C1#series:1#eb:0"], "count": 2}`.
* Exit `0` hit / `2` usage / `5` no hit — and a miss still prints **parseable JSON naming the
  searched scope**, so a "not found" answer is evidence, not silence.
* Copy the id from `mark_data_map.json` or the `locate` output: ids are
  `<chart-id>#<series-group>#<family>:<index>` (e.g. `C1#series:0#bar:0`,
  `C2#series:0#pt:3`, `C1#series:0#eb:0`); a bare `series:0#bar:0` is accepted when the suffix
  matches exactly one mark, and `--file/--row/--col` resolves the table name from the map.
* Point a user at `references/provenance-and-evidence.md` for the full contract; never hand-write a
  mapping, and never edit `mark_data_map.json`.

### 8. Journal presets

```
python3 scripts/chart.py presets --list
python3 scripts/chart.py build --spec … --preset cell
```

Presets: `nature-single`, `nature-double`, `science`, `cell`, `elsevier`, `ieee`, `generic`
(each provides canvas mm, dpi, font sizes, line widths, palette, margins, panel-label style,
font stack with CJK fallback, tick style, and a `notes/source` field stating where the numbers
come from — a journal guideline or `convention`). An **unknown preset id is `E916`** and never
silently falls back to the default. See [`references/presets.md`](references/presets.md).

### 9. Edit without losing provenance

```
python3 scripts/chart.py edit --spec /app/work/spec.json --edits edits.json --out /app/work/spec2.json
python3 scripts/chart.py build --spec /app/work/spec2.json --out /app/output
python3 scripts/chart.py result --out /app/output --input /input --work /app/work
python3 scripts/chart.py check  --out /app/output --spec /app/work/spec2.json --work /app/work
```

`edit` supports the frozen ops `set_value`, `set_label`, `set_unit`, `add_series`,
`remove_series`, `recolor_series`, `set_preset`, `set_axis`, `set_error_kind`,
`set_significance`, `set_sample_size`, `set_colorbar`, `set_title`. Each op appends a
`change_records` entry `{index, op, target, before, after, status, note}`; the rebuild records the
artifact sha256 change and `provenance_preserved`. **Never edit an export by hand** — edit the
spec and rebuild, or the mapping and the gates become lies. See
[`references/runbook.md`](references/runbook.md) §5.

## When a gate fails

Never "fix" the figure by editing an export. Read `validation_report.json` → `checks[]`
(`status: fail`) and `failed[]`, then act on the cause:

| Symptom | Exit | Do this |
|---|---|---|
| Spec rejected structurally | `4` | Read `spec-issues.json` (next to the output), fix `spec.json`, rebuild. |
| Unknown preset | `4`/`2` | **E916** — print the available ids (`presets --list`) and pick one; do not fall back silently. |
| Gate **A** overrun | `1` | Enlarge the canvas, shorten a title, or let the layout resolve the page; check `margin_min`. |
| Gate **B** text overflow / too-small font | `1` | Shorten the label or sublabel; reduce `meta.layout.columns`; never shrink below the preset's floor. |
| Gate **C** colour | `1` | Switch `palette.id` to `okabe-ito`; use ≤3 series or distinct shapes/hatches for a fourth. |
| Gate **D** encoding mismatch | `1` | Make `legend.items` exactly the data categories, fix `colorbar.vmin/vmax` to the data range, add the missing `unit`. |
| Gate **E** provenance | `1` | `E903` no `source` → add a citation id; `E904` id does not exist → fix the evidence item; `E906` value not in its quote → find material that really contains it, or drop the number and record the gap in `limitations`/`uncertainty`. **Do not invent a quote and do not change the number to match the quote.** |
| Gate **F** editability | `1` | The build is broken — rebuild; if it persists report it, do not rasterise. |
| Gate **G** determinism | `1` | A non-deterministic input leaked into export (time, `/ID`, dict order, RNG). Remove it. Gate G only re-exports; `chart.py selftest` tests the whole chain. |
| Gate **H** export | `1`/`2` | Rebuild; if fonts cause it, install a CJK-capable font and record the fallback in `limitations`. |
| Gate **I** contract | `1` | Check the exact `output_contract` key names; drop the `--status` override and re-run `result`. |
| Gate **J** mark/source (`E910`) | `1` | For each offending mark in `validation_report.json`: bind `series[].values` to the real column, add `source`/`sources`, or mark it `derived` with the exact `source_values` (tolerance `1e-9`). Never delete the mapping to make the gate green. |
| Gate **K** annotation (`E911`–`E915`) | `1` | `E911` add `error_kind` (state what the bar is: sd/sem/ci95/iqr/range); `E912` add the test name the source gives, or `defined_in_caption: true` and define the comparison in the caption (then `p` is optional); `E913` add `n`; `E914` add the axis `unit`; `E915` set `baseline: 0` or supply `justification`. |
| Gate **L** CJK (`E917`) | `1` | Use a font that contains the glyph, or state plainly in `limitations`/`uncertainty` that the glyph is unavailable and `status` is `partial`; **do not draw tofu boxes and call it pass**. |
| Warn `W920`/`W922`/`W923` on a graded figure | `0` | Fix if cheap: widen the tick range, align the caption with the drawn categories, reduce overlap. Warns do not fail, but they are the difference between "pass" and "good". |
| Artefacts missing | `3` | Run `build` before `check`/`result`. |
| `locate` miss | `5` | Read the JSON's `searched` scope; a miss means the mark/cell is not in `mark_data_map.json` — re-run `ingest` + `build`. |
| Module/usage error | `2` | Check the CLI invocation against `chart.py <cmd> --help`. |

If after two repair loops a gate still fails, **stop and ship honestly**: run
`chart.py result --status partial`, put the exact failing gate id and the reason into
`uncertainty`, and tell the user what is unresolved. A partial result that names its limits
scores; a silent fabrication does not.

## Do not fabricate (hard rule)

The mark→cell mapping and the evidence ledger are the contract between the figure and reality:

* **Every drawn mark, label, number, unit, `n`, error kind and significance marker carries a
  source.** Gate J fails (`E910`) when a drawn value cannot be located in a real material cell
  within `1e-9` (and is not a declared derivation with `source_values`); gate E fails
  (`E903`–`E906`) on a missing, dangling or non-verbatim citation.
* If a material does not contain the value you need, you may **not** supply one. Omit the mark,
  or state the gap in `limitations` / `uncertainty`.
* Never invent a condition name, a unit, a time point, an `n`, a p-value or a significance
  star; never restate a number you did not read; never let a placeholder survive into the output.
* Never compute a p-value, a test statistic or a model fit that the material does not state — this
  Skill is deliberately not a statistics package.
* Never present the bundled synthetic fixture or a rendered example as an official case result.
  Never claim a benchmark score or a runtime you did not measure.
* Never report PNG/PDF/PPTX/EPS/TIFF as delivered when the backend was missing: mark it
  `unavailable` **with the reason**, and keep the claim out of `artifacts`.
* When material is missing, ambiguous or contradictory, say so in `uncertainty` and set `status`
  to `partial` (or `needs_human`); that is a correct, gradeable outcome.

## Verify the Skill itself

```
python3 scripts/chart.py selftest        # full-chain determinism + injected-defect negatives
python3 scripts/chart.py presets --list  # the shipped preset ids
python3 scripts/selftest.py              # same suite via the direct entry point
python3 scripts/contract_matrix.py       # adversarial output_contract key sets
python3 scripts/gain_check.py            # measured 使用增益 A/B
```

`presets --list` must exit `0`; the four harness commands (both `selftest` entry points,
`contract_matrix`, `gain_check`) are required to exit `0` as well, and
[`references/rubric-mapping.md`](references/rubric-mapping.md) §"Execution evidence" records the
measured status of each with its command — quote that page rather than assuming, and never report a
harness as green without its transcript. `contract_matrix` matters most for an **unseen** case:
the evaluator's case may name its outputs anything, and an unknown contract key is filled with a
truthful run summary and disclosed in `audit.unmapped_contract_keys` rather than silently
emptied. `gain_check` measures defect interception (a fabricated value, a dangling citation, a
truncated bar baseline, an undeclared error bar) — it is **not** a model-quality benchmark.

## Reference files

| File | Read it for |
|---|---|
| [`references/spec-schema.md`](references/spec-schema.md) | every `spec.json` field, including the chart/axis/errors/significance fields, and its validation code |
| [`references/runbook.md`](references/runbook.md) | the exact command sequence, exit codes, budget plan, recovery, edit loop |
| [`references/provenance-and-evidence.md`](references/provenance-and-evidence.md) | `mark_data_map.json`, `locate`, `source_map.json`, gates E/J, how a quote/value is matched |
| [`references/data-chart-design.md`](references/data-chart-design.md) | choosing a chart type from the table shape, and the annotation set gate K demands (axis/unit/n/error/significance) |
| [`references/presets.md`](references/presets.md) | the seven journal presets, their parameters and their stated sources |
| [`references/exports-fonts-colour.md`](references/exports-fonts-colour.md) | canvas/dpi/sRGB/PDF/PPTX, font embedding, CJK fallback, colour accessibility, determinism |
| [`references/failure-modes.md`](references/failure-modes.md) | observed behaviour for each failure class (exit code, gate, `result.json`) |
| [`references/measured-gain.md`](references/measured-gain.md) | the measured A/B for 使用增益 — defect interception and delivered verification artefacts, and what it does **not** measure |
| [`references/rubric-mapping.md`](references/rubric-mapping.md) | which artefact + command proves each scoring point |
| [`README.md`](README.md) | dependencies, install to `/skills/weekly-skill`, reuse, licence, resource budget |
| [`examples/README.md`](examples/README.md) | the worked synthetic example and its rendered artefacts |

## Runtime contract reminder

The platform installs this Skill at `/skills/weekly-skill`, runs it with the official DeepSeek
Harness against a case whose input is `/input/case.json` (+ `/input/task.md` +
`/input/materials/**`) and expects the outputs under `/app/output`, with `result.json` echoing
`case_id` and every `output_contract` key. Nothing in this Skill reads or writes outside
`/input`, `/app/work` and `/app/output`.

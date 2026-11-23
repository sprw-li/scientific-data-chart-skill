# Examples

This directory demonstrates the Skill end to end on a **synthetic** fixture.

> ## ⚠️ SYNTHETIC EXAMPLE — NOT AN OFFICIAL CASE
> `synthetic-001` is **invented material**: a fictional hypoxia / cardiomyocyte study whose numbers
> were made up to exercise the pipeline. It must never be presented or cited as a real
> measurement, and nothing here is an official challenge case or an official result.
> It is also **not** the same thing as the test fixtures used by the authors (those are separate
> corpora for negative testing and are not shipped): this directory is the reader-facing worked
> example of the interface.

## What is in here

```
examples/
├── README.md                     <- you are reading it
└── synthetic-001/
    ├── input/                    <- the case exactly as the runtime would receive it
    │   ├── case.json             <- case_id, figure block (incl. a stale_caption trap), output_contract
    │   ├── task.md               <- the work order, labelled SYNTHETIC
    │   └── materials/
    │       ├── experiment.csv    <- per-replicate viability table (3 conditions x 3 replicates)
    │       ├── timecourse.csv    <- ROS time course (2 conditions x 4 time points, n = 3 each)
    │       └── methods.md        <- the authoritative measurement note (units, n, error-bar meaning)
    └── expected/                 <- outputs of a real run of this Skill on the input above
        ├── spec.json             <- the authored, validated spec (the editable source of the science)
        ├── figure.svg            <- the editable rendered source (UTF-8 text)
        ├── result.json           <- the structured case result written by `chart.py result`
        ├── validation_report.json<- gates A–L with their measured detail
        └── mark_data_map.json    <- the two-way mark <-> source-cell map (gate J)
```

(`source_map.json` is produced by every run but is **not stored here** — its shape is documented in
[`references/provenance-and-evidence.md`](../references/provenance-and-evidence.md) §5, and the
bundle is held under the platform's 50-file ceiling.)

Only **UTF-8 text** ships in this bundle, so the raster/vector binaries are not stored here. Their
measured properties are recorded in `export_report.json` of a real run and summarised below.

## What this fixture exercises

It is deliberately small but touches every new part of the contract:

* **A bar chart** (`type: "bar"`) of cell viability per condition. Each condition is **its own
  series with one value** (the one-bar-per-condition shape from CONTRACT §6.1), so `categories` is
  deliberately omitted — the category label is the series name, and every label occurs verbatim in
  the table. `baseline: 0`, a `%` unit on the y axis and an explicit `axis.y` tick range.
* **Derived values done honestly**: each bar is the mean of three replicate rows, so every series
  carries `derived: true` and lists its inputs in `source_values`, with **all three** rows cited
  through `sources`.
* **Error bars with a declared meaning** (`errors` + `error_kind: "sd"`) and a **reported sample
  size** (`n: 3`), both stated in the material (`materials/methods.md`, lines L6–L7) — that is
  gates K's `E911`/`E913` surface.
* **A significance bracket without an invented p-value**: the source note reports that hypoxia
  differs from the control and explicitly does **not** state a p-value, so the spec marks the
  reported comparison with `defined_in_caption: true` instead of fabricating a `p` — the `E912`
  surface.
* **A long category label** (`hypoxia+siHIF1A`) is kept on purpose: the skill must wrap or fit it
  rather than drop it (gates B/F and the "label too long" handling).
* **A line chart** (`type: "line"`) with `series[].x` time points and an `x_unit` of `h`.
* **A stale-caption trap**: `case.json.figure.stale_caption` names the wrong unit (kg), the wrong
  statistic (median) and the wrong number of conditions (two). It is metadata, not source data;
  the spec uses `methods.md` + the tables.
* **One deliberately unrecognised `output_contract` key** (`reviewer_notes`): `result.json` fills
  it with an honest run summary and discloses it in `audit.unmapped_contract_keys` instead of
  leaving it empty.
* **22 evidence items**, one per source row plus one per authoritative methods sentence, each with
  a verbatim `quote` and a resolvable `locator` (`row 2`…`row 10`, `row 2`…`row 9`, `L5`…`L12`).

## Run it yourself

From the submission root:

```
python3 scripts/chart.py ingest --input examples/synthetic-001/input --work /tmp/chart-work
cp examples/synthetic-001/expected/spec.json /tmp/chart-work/spec.json
python3 scripts/chart.py build  --spec /tmp/chart-work/spec.json --out /tmp/chart-out --work /tmp/chart-work --preset nature-single
python3 scripts/chart.py result --out /tmp/chart-out --input examples/synthetic-001/input --work /tmp/chart-work
python3 scripts/chart.py check  --out /tmp/chart-out --spec /tmp/chart-work/spec.json --work /tmp/chart-work
python3 scripts/chart.py result --out /tmp/chart-out --input examples/synthetic-001/input --work /tmp/chart-work
```

The order matters: gate **I** reads `result.json`, so `check` must come after the first `result`
(and the second `result` folds the gate verdict back into `status`).

Then walk the two-way mapping:

```
python3 scripts/chart.py locate --out /tmp/chart-out --mark "series:0#bar:0"
python3 scripts/chart.py locate --out /tmp/chart-out --file materials/experiment.csv --row 5 --col cell_viability_pct
```

Both must exit `0` and agree on the same cell.

To see the no-LLM fallback instead of the authored spec, skip the `cp` and run:

```
python3 scripts/chart.py run --input examples/synthetic-001/input --work /tmp/chart-work --out /tmp/chart-out
```

`autospec` derives a structurally valid, deliberately conservative spec — good enough to produce
artefacts and prove the machinery, **not** a substitute for an authored spec.

## How to read `expected/`

| File | What to do with it |
|---|---|
| `spec.json` | the template: copy it and rewrite the science for your own case. Validates with **0 errors, 0 warnings** (`scene.validate_spec`). |
| `figure.svg` | open it in a browser or Inkscape; check that groups are named, text is selectable/editable, no whole-image bitmap is present, and each mark carries its provenance attributes. |
| `result.json` | the shape the evaluator reads back: `case_id`, `status`, `artifacts` (path/bytes/sha256), `checks`, `preset`, `mark_data_map`, `source_map`, `sources`, `evidence`, `audit`, `uncertainty`, `limitations`. Produced by a real run, not hand-written. |
| `validation_report.json` | the per-gate verdicts with the measured `detail` strings quoted throughout this repository. |
| `mark_data_map.json` | the two-way map: `marks[]` with `source.cell` + `source_values`, `cells{}` reverse index, `unmapped[]`/`ambiguous[]`. |
| `source_map.json` | produced by the run but **not stored here** (50-file ceiling); its shape — `schema_version`, `case_id`, `universe`, `counts`, `elements[]{element_id,kind,layer,panel_id,source,evidence_ids,value,text}` — is documented in [`references/provenance-and-evidence.md`](../references/provenance-and-evidence.md) §5. |
| `figure.png` / `figure.pdf` | **not stored** in the bundle (binaries; the bundle is UTF-8 text only) — their measured properties are in "Rendered exports" below. |

## Rendered exports (not stored in the bundle)

`figure.png` (≥300 dpi, sRGB, pixel size == page × dpi) and `figure.pdf` (1 page, vector, embedded
fonts, selectable text) are produced by the same build. Because the bundle must contain only
UTF-8 text (and ≤500 KB per file), those binaries live outside it; reproduce them with the `build`
command above and compare against `export_report.json`, which records the measured values by
re-reading the artefact bytes. **Measured on a real run (2026-11-23 ~13:0x +08:00):**

| Property | Measured | Expected / threshold |
|---|---|---|
| page | `[120.0, 90.0]` mm | the spec's and `case.json.figure.canvas`'s `[120.0, 90.0]` — off `0.00` mm |
| PNG pixels | `[1417, 1063]` | `expected_px [1417, 1063]` — equal |
| PNG dpi metadata | `300.0248` (`pHYs` present) | ≥ 300 (pixel size is the binding check) |
| PNG bytes / sha256 | `231,913` / `c6308839b5a26400…` | — |
| PDF pages | `1` | `1` |
| PDF fonts | `2` embedded font programmes | `> 0` embedded |
| PDF vector text | `69` `Tj`/`TJ` operators, `vector=True` | `> 0` |
| PDF rasterisation | `0` image objects | informational |
| PDF MediaBox | `[340.1575, 255.1181]` pt | `[340.1575, 255.1181]` — equal |
| PDF bytes / sha256 | `1,102,143` / `4010ef82afebb808…` | — |

## Run status

The pipeline is developed by several teammates in parallel; this table records what has actually
been executed on this fixture and what has not, so nothing here is claimed without evidence. All
rows were produced on **2026-11-23 ~13:0x +08:00** from the revision then in the tree.

| Claim | Status |
|---|---|
| `spec.json` passes `scene.validate_spec` | **executed** — `0 errors, 0 warnings` (22 evidence items, 2 panels, 2 charts) |
| `chart.py run` with the authored spec in `W/spec.json` | **executed** — **exit `0`**, `check: all checks passed (1 warn)`; `result.json` `status: ok` |
| gates A–L | **executed** — `checks = {A..L: pass, K: warn}`, `audit.checks_failed = []`; K's single warn is `W922` (the case's stale caption naming `kg` while the drawn units are `%`/`fold`) |
| gate **J** numbers | **executed** — `marks=15, numeric_marks=14, derived_marks=6, typological_marks=1(located=1), cells_index=17, unmapped=0, ambiguous=0, value_mismatch=0, coverage_missing=0, source_missing=0` |
| gate **E** numbers | **executed** — `materials=5, refs=17, dangling=0, evidence_items=22, quotes_resolved=22, quotes_unresolved=0, module_issues=0` |
| gate **K** numbers | **executed** — `violations=0, warnings=2`, `errbar_series=3, series_with_n=5, sig_defined_in_caption=1` |
| gate **G** determinism | **executed** — `identical=True` for SVG/PNG/PDF on re-export |
| **two-way `locate`** | **executed** — `--mark "C1#series:1#bar:0"` → `materials/experiment.csv#experiment:5:cell_viability_pct`, exit `0`; `--cell "materials/experiment.csv#experiment:5:cell_viability_pct"` → `["C1#series:1#bar:0","C1#series:1#eb:0"]`, exit `0`; a miss exits `5` with a parseable `searched` scope |
| `chart.py run` on an **empty** work dir (the platform's autospec path) | **executed** — **exit `0`**, `all checks passed (2 warn)`, `status: ok`, `audit.checks_failed=[]`, warns `C`/`K`; autospec emits `3 panel(s), 17 evidence`, and the map has `16 marks / 26 cells / 0 unmapped / 0 ambiguous` |
| bundle structure (`validate_skill_submission.py .`) | **executed** — `status: ok, files: 50, bytes: ~1.12 MB, errors: []` (files and bytes move with any edit; the binding limits are ≤50 files, ≤500 KB/file, ≤2 MB total) |
| `chart.py selftest` | **executed** — **exit `1`**: its own `chart-run-exit-0` case finishes with `check exit=1 :: FAILED 1 check(s): J` while this example's own run passes J (`unmapped=0, ambiguous=0`), so the failure is in the harness fixture, not in the example. Lead-owned script, reported — see [`references/rubric-mapping.md`](../references/rubric-mapping.md) |
| `contract_matrix.py` | **executed** — **exit `0`, 5/5 PASS** including the empty `output_contract` case |
| `gain_check.py` | **executed** — **exit `0`**; detection `A 0/3` vs `B 2/3` (one injector `[SKIP]`, text overflow escapes both) — see [`references/measured-gain.md`](../references/measured-gain.md) |

Never trust a number in this directory that is not accompanied by the command that produced it.
The files in `expected/` (`figure.svg`, `result.json`, `validation_report.json`,
`mark_data_map.json`) are copied verbatim from the output directory of that run; `figure.svg` is
not hand-written and re-running the pipeline with the same spec reproduces gate G's byte-identical
result. `source_map.json` comes out of the same run but is not stored here (see above).

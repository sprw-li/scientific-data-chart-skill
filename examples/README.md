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
        └── result.json           <- the structured case result written by `chart.py result`
```

Only **UTF-8 text** ships in this bundle, so the raster/vector binaries are not stored here. Their
measured properties are recorded in `export_report.json` of a real run and summarised below.

## What this fixture exercises

It is deliberately small but touches every new part of the contract:

* **A grouped bar chart** (`type: "bar"`) of cell viability per condition, with
  `categories` taken verbatim from the table, `baseline: 0`, a `%` unit on the y axis and an
  explicit `axis.y` tick range.
* **Derived values done honestly**: each bar is the mean of three replicate rows, so every series
  carries `derived: true` and lists its inputs in `source_values`, with **all three** rows cited
  through `sources`.
* **Error bars with a declared meaning** (`errors` + `error_kind: "sd"`) and a **reported sample
  size** (`n: 3`), both stated in the material (`materials/methods.md`, lines L6–L7) — that is
  gates K's `E911`/`E913` surface.
* **A significance bracket without an invented p-value**: the source note reports that hypoxia
  differs from the control and explicitly does **not** state a p-value, so the spec marks the
  reported comparison (`defined_in_caption`) instead of fabricating a `p` — the `E912` trap.
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
| `result.json` | the shape the evaluator reads back: `case_id`, `status`, `artifacts` (path/bytes/sha256), `sources`, `evidence`, `preset`, `checks`, `mark_data_map`, `source_map`, `audit`, `uncertainty`, `limitations`. Produced by a real run, not hand-written. |

## Rendered exports (not stored in the bundle)

`figure.png` (≥300 dpi, sRGB, pixel size == page × dpi) and `figure.pdf` (1 page, vector, embedded
fonts, selectable text) are produced by the same build. Because the bundle must contain only
UTF-8 text, those binaries live outside it; reproduce them with the `build` command above and
compare against `export_report.json`, which records the measured pixel size, `pHYs` dpi, page
count, font programmes, vector-text operator counts, `MediaBox` and sha256 of each artefact.

## Run status

The pipeline is developed by several teammates in parallel; this table records what has actually
been executed on this fixture and what has not, so nothing here is claimed without evidence.

> **Status: pending the implementation freeze.** The table is filled from the frozen bundle; until
> a row carries a command and its printed result it must be treated as *intended*, not *proven*.

| Claim | Status |
|---|---|
| `spec.json` passes `scene.validate_spec` | **executed** — `0 errors, 0 warnings` (22 evidence items, 2 panels, 2 charts) |
| `chart.py ingest` on `input/` | _to be filled_ |
| `chart.py build` + `check` on the authored spec | _to be filled_ |
| gate **J** (marks ↔ cells) and gate **E** (citations) | _to be filled_ |
| two-way `locate` agreement | _to be filled_ |
| `chart.py run` on `input/` (autospec path) | _to be filled_ |
| whole-chain determinism (`selftest`) | _to be filled_ |
| bundle structure (`validate_skill_submission.py .`) | _to be filled_ |

Never trust a number in this directory that is not accompanied by the command that produced it.

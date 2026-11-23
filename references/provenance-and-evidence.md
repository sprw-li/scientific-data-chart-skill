# Provenance and evidence — the two-way link between the figure and the data

This Skill's core claim is not "a nice chart". It is: **every drawn mark can be walked back to
the exact cell it came from, and every cell can be walked forward to the marks drawn from it.**
That link is produced mechanically and is checked by gates **E** and **J**.

Sources of truth: `work/CONTRACT.md` §1/§2/§4/§7 (frozen) and `scripts/evidence.py`,
`scripts/datamodel.py`, `scripts/datamap.py`, `scripts/validate.py`.

---

## 1. The chain

```
/input/materials/**                                   (bytes on disk)
      |  chart.py ingest
      v
work/materials.json        per-file text + sha256 + locator index
work/data-tables.json      typed tables: file / table / header_row / n_rows / columns / rows
work/evidence-ledger.json  candidate evidence items (id, claim, value, unit, source{file,locator,quote})
      |  you author spec.json from these
      v
work/spec.json             series[].values + source/sources + column + source_values + n + error_kind
      |  chart.py build
      v
output/scene.json          laid-out geometry; every mark carries a stable mark_id
output/mark_data_map.json  marks[].source{file,table,row,column,cell,quote} + cells{} reverse index
output/source_map.json     element-level provenance (file/locator/quote/value)
      |  chart.py check
      v
output/validation_report.json   gate E (citations) + gate J (mark<->cell coverage/value match)
```

Two independent obligations:

* **Gate E** — the *citation* is real: the evidence item's `source.file` exists, its `locator`
  resolves to a span, and its `quote` occurs **verbatim** in that span; each element has a
  citation; every printed number occurs in its cited quote (or its `source_values` do, when
  derived).
* **Gate J** — the *data binding* is real: the drawn marks cover the drawn geometry, each mark's
  `source.cell` locates in a real table cell, the mark `value` equals that cell within `1e-9` (or
  is a declared derivation whose inputs are the mapped cells), the reverse index covers every
  mark, and category labels occur verbatim in the material.

Gate E can be satisfied by a well-written quote while gate J still fails, and vice versa: E is
about *citing* the material, J is about *binding the drawing to the data*. A figure that passes
both is one a reviewer can audit cell by cell.

---

## 2. What `ingest` produces

`chart.py ingest --input /input --work /app/work` writes:

| File | What is in it |
|---|---|
| `materials.json` | one entry per material: `path`, `kind`, `bytes`, `sha256`, extracted text, and a **locator index** (`L<n>` for text lines, `row <n>` for CSV/TSV records, header = row 1). |
| `data-tables.json` | one entry per parsed table: `{file, sha256, format, tables:[{name, header_row, n_rows, columns, rows}]}`. This is what you bind `series[].values` to — read it, do not guess from the filename. |
| `evidence-ledger.json` | candidate claims with resolvable spans (`items[]`), each with `resolved{ok, file, locator, span}`. |

Extraction quality is recorded, not hidden: a material that could not be parsed, a PDF with no
text layer, an empty table, a non-numeric cell inside a numeric column — each is reported with a
reason and flows into `limitations`/`uncertainty` rather than being silently dropped.

`ingest` exits `0` on success and `2` on usage/IO. A material that is simply unreadable is **not**
an exit-2 crash: it is a recorded gap, because a partially usable input set is a normal case.

---

## 3. `mark_data_map.json` — the frozen schema

```jsonc
{
  "schema_version": "1.0",
  "case_id": "...",
  "materials": [{"file": "materials/experiment.csv", "sha256": "...", "format": "csv",
                 "tables": [{"name": "experiment", "header_row": 1, "n_rows": 9,
                             "columns": ["condition","replicate","time_h","cell_viability_pct", "..."]}]}],
  "marks": [
    {"mark_id": "series:0#bar:1",              // stable id
     "chart_id": "C1", "panel_id": "P1",
     "series": "hypoxia", "category": "hypoxia", "value": 83.13,
     "derived": true,
     "source": {"file": "materials/experiment.csv", "table": "experiment",
                "row": 5, "column": "cell_viability_pct",
                "cell": "materials/experiment.csv#experiment:5:cell_viability_pct",
                "row_label": "hypoxia", "quote": "hypoxia,1,24,82.1,2.74,12.6,0.19"},
     "source_values": [82.1, 84.3, 83.0]}
  ],
  "cells": {"materials/experiment.csv#experiment:5:cell_viability_pct": ["series:0#bar:1"]},
  "unmapped": [],
  "ambiguous": []
}
```

### 3.1 Mark id naming (frozen)

| Form | Used for |
|---|---|
| `<series-group>#bar:<i>` | the i-th bar of a series group |
| `<series-group>#pt:<i>` | the i-th point / marker of a series |
| `<series-group>#cell:<r>,<c>` | a heatmap cell (row, column) |
| `<series-group>#eb:<i>` | the i-th error bar |

Ids are **stable**: the same spec + the same materials produce the same id, so a `locate` result
can be quoted in a bug report, an edit log or a review.

### 3.2 Coverage rules (gate J, error `E910`)

1. Every mark actually drawn (bar, point, line marker, heatmap cell, error bar, significance
   bracket) appears in `marks[]`.
2. Every `marks[].source.cell` locates in the real material named by `source.file`.
3. `|marks[].value − cell_value| ≤ 1e-9`. A **derived** value (a group mean, a ratio, a
   normalisation) must set `"derived": true` and list the inputs in `source_values`; the
   derivation must be reproducible from the mapped cells.
4. `cells{}` covers every mark's `cell` (**the reverse index is complete**).
5. A categorical/pictorial mark may point at a header or row label instead of a numeric cell, but
   the label must occur **verbatim** in the material.
6. Numeric marks are never allowed to be "source-less". `unmapped`/`ambiguous` entries are
   honest reports of what could not be bound — they are not a place to hide a fabricated value.

---

## 4. `locate` — the bidirectional query

```
python3 scripts/chart.py locate --out /app/output --mark "series:0#bar:1"
python3 scripts/chart.py locate --out /app/output --cell "materials/experiment.csv#experiment:5:cell_viability_pct"
python3 scripts/chart.py locate --out /app/output --file materials/experiment.csv --row 5 --col cell_viability_pct
```

Forward (`--mark`) returns:

```jsonc
{"mark": "series:0#bar:1", "chart_id": "C1", "panel_id": "P1", "series": "hypoxia",
 "value": 83.13, "derived": true,
 "source": {"file": "materials/experiment.csv", "table": "experiment", "row": 5,
            "column": "cell_viability_pct", "cell": "...", "quote": "hypoxia,1,24,82.1,2.74,12.6,0.19"},
 "evidence": ["E4", "E5", "E6"]}
```

Reverse (`--cell`, or `--file/--row/--col`) returns the marks drawn from that cell:

```jsonc
{"cell": "materials/experiment.csv#experiment:5:cell_viability_pct",
 "file": "materials/experiment.csv", "table": "experiment", "row": 5,
 "column": "cell_viability_pct", "value": 82.1,
 "marks": ["series:0#bar:1", "series:0#pt:0"]}
```

Exit codes: `0` hit · `2` usage · `5` no hit. **A miss is still a parseable JSON document** that
names the searched scope (`searched`, the map's `case_id`/`schema_version`, and what was looked
for), so "not found" is evidence rather than silence. Never edit `mark_data_map.json` by hand —
re-run `build` after changing `spec.json`.

### Worked answer for a user

> "Which rows is the hypoxia bar built from?"

1. `locate --mark series:0#bar:1` → `source.cell`, `source.quote`, `source_values`.
2. Open that file at the named row/column; the quote is byte-identical.
3. Reverse-check with `locate --cell …` → the mark list comes back.

---

## 5. `source_map.json`

Element-level provenance for the rest of the figure (titles, axis labels, units, legend entries,
`n=` annotations, categories): `{element, file, locator, quote, value}`. Gate E uses it together
with `spec.evidence[]` so that an annotation is as traceable as a bar.

---

## 6. Gate E, exactly

| Code | Severity | Condition |
|---|---|---|
| `E903` | error | an element (node/edge/series/annotation) has no `source`/`sources` at all |
| `E904` | error | the cited id does not exist in `spec.evidence[]` (dangling citation) |
| `E905` | error | the `source` does not resolve to a real span, or the `quote` is not verbatim in it |
| `E906` | error | a printed number does not occur in its cited quote |
| `E907` | warn | `derived: true` without recomputable `source_values` — the number is then not value-checked at all |

How a number is matched: gate E looks for the rendered value under integer/plain/`%g`/`%.1f`/
`%.2f`/`%.3f` forms with digit boundaries, so `2.8` matches `2.8` but not `12.85`. A mean of
`83.133…` printed as `83.13` will **not** be found in a row that contains `82.1` — this is why a
computed value must be declared `derived` with `source_values`, and why the mean's inputs (not
the mean) are what must appear in the cited quotes.

---

## 7. What lands in `result.json`

* `evidence` — the resolved ledger (`id`, `claim`, `kind`, `source`, `value`, `unit`, `resolved`).
* `sources` — every material file with `path`, `kind`, `role`, `sha256`, `bytes`.
* `mark_data_map` / `source_map` — the filenames of the two maps.
* `audit.checks_ok` / `checks_failed` / `checks_warned` — the gate letters, so a reader sees at a
  glance whether J and E passed.
* `uncertainty` — one entry per failed gate plus one per structural gap; `limitations` — the
  honest state of fonts, missing backends and material gaps.

A run whose gate J failed can never report `status: "ok"`; the failure is copied into
`uncertainty` with the measured detail.

---

## 8. Honesty rules (enforced, not advisory)

* Never invent a quote, and never change a number to match a quote. If the material lacks the
  value, omit the mark and record the gap.
* Never delete a `source` to make gate J green, and never hand-edit `mark_data_map.json`.
* Never present `unmapped`/`ambiguous` as "nothing to see" — report counts and what was searched.
* A claimed significance marker must come from the material (`test` + `p`, or a caption that
  defines it). This Skill **never computes** a p-value or a test statistic.

---

## 9. Reproducing the audit by hand

```
python3 scripts/chart.py ingest --input examples/synthetic-001/input --work /tmp/w
python3 scripts/chart.py build  --spec examples/synthetic-001/expected/spec.json --out /tmp/o --work /tmp/w
python3 scripts/chart.py locate --out /tmp/o --mark "series:0#bar:0"
python3 scripts/chart.py locate --out /tmp/o --file materials/experiment.csv --row 5 --col cell_viability_pct
python3 scripts/chart.py check  --out /tmp/o --spec examples/synthetic-001/expected/spec.json --work /tmp/w
python3 -c "import json;d=json.load(open('/tmp/o/mark_data_map.json'));print(len(d['marks']),len(d['cells']))"
```

The two `locate` calls must exit `0` and print the same cell from both directions; `check` must
print gate **J** and gate **E** as `pass` with measured counts (`marks=N, cells=N, mismatches=0`).

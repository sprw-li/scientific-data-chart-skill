# `spec.json` schema reference

`spec.json` is the **only** hand-authored input to the pipeline: you write the science
communication (chart type, categories, which column feeds `values`, units, `n`, error meaning,
significance) and the scripts do everything else deterministically.

Sources of truth: `work/CONTRACT.md` §6 (frozen interface) plus `scripts/scene.py`
`validate_spec()` (structural codes). Where this file disagrees with either, the code wins —
report the mismatch, do not guess.

Legend for the tables: **req** = validator error when missing, *rec* = warn only, *opt* = free.

---

## 1. Top level

| Key | Type | Rule |
|---|---|---|
| `meta` | object | **req** (`E001`). |
| `palette` | object | optional; `W010` when no `id`. |
| `texts` | object | optional free-form strings used by `result.json` contract mapping. |
| `evidence` | array | **req**, non-empty (`E020`). |
| `panels` | array | **req**, non-empty (`E030`). Charts live inside panels. |
| `uncertainty` | array | optional; copied into `result.json`. |
| `limitations` | array | optional; copied into `result.json`. |
| `change_records` | array | written by `edit`; do not author by hand. |

> **`charts` exist only at `panels[].charts[]`** (frozen: no top-level `charts` key). A single
> chart figure is therefore a single panel. A chart that is not inside a panel is not drawn.

---

## 2. `meta`

| Key | Type | Rule |
|---|---|---|
| `case_id` | string | **req**, non-empty (`E002`). |
| `language` | string | *rec* (`W012`); e.g. `en`, `zh`. Drives font resolution. |
| `preset` | string | optional; one of the seven ids (`E916` at build when unknown). |
| `figure_id` | string | optional; e.g. `figure03`. |
| `title` | string | optional; main title. |
| `caption` | string | optional; figure caption (gate K compares it with the drawn categories). |
| `canvas` | object | optional; defaults below. |
| `layout` | object | optional; `columns` (int, default 2), `row_order` (legacy role order). |

### `meta.canvas`

| Key | Default | Range | Code |
|---|---|---|---|
| `w_mm` | 180 | 20–1000 | `E003` |
| `h_mm` | 120 | 20–1000 | `E004` |
| `dpi` | 300 | 72–1200 | `E005` |
| `margin_mm` | 3 | 0–20 | `E006` |

Margins must leave a drawing area (`E007`). `--dpi` on `build` overrides `meta.canvas.dpi` and is
written into `figure_spec.resolved.json` and `export_report.json`.

### `meta.layout` (legacy, still read)

`{"columns": 2, "row_order": ["problem","method","results","conclusion"]}`. For a pure data
chart you normally let the layout auto-place charts and set only `columns`.

---

## 3. `palette`

```jsonc
"palette": {"id": "okabe-ito"}          // CVD-safe categorical order (default)
"palette": {"id": "custom", "colors": ["#0072B2", "#E69F00", "#009E73"]}
```

* Known ids: `okabe-ito`, `grayscale-safe`. An unknown id **with** a `colors[]` list is accepted;
  an unknown id **without** one is `E011`.
* The categorical order is chosen so the first four colours differ in L\* as well as hue; gate C
  reports measured ΔE/ΔL\* and fails below its floors (see `exports-fonts-colour.md`).
* Sequential ramps for heatmaps/colorbars are separate: `viridis`, `batlow`, `cividis`, `grey`.

---

## 4. `texts`

Free-form strings that `chart.py result` maps onto `output_contract` keys. Keep them non-empty —
an empty value is treated as "not derivable" and can push `status` to `partial`.

| Key | Used for contract keys containing |
|---|---|
| `title` | `title`, `heading` |
| `caption` | `caption`, `figure_legend` |
| `legend_note` | `legend` (fallback) |
| `conclusion` / `summary` | `summary`, `abstract`, `answer`, `narrative` |

---

## 5. `evidence[]` — the provenance ledger

Required and non-empty. Every drawn number, category label, unit, `n`, error kind and
significance marker that reaches the figure should resolve here. Gate E re-checks every citation
against the real material bytes.

| Key | Type | Req | Rule |
|---|---|---|---|
| `id` | string | **yes** | Non-empty (`E022`), unique (`E023`); referenced by `source`/`sources`. |
| `claim` | string | **yes** | Non-empty (`E024`). |
| `kind` | string | rec | `W028`; suggested `entity` \| `relation` \| `number` \| `condition`. |
| `value` | number/null | opt | Displayed value (e.g. `2.83`). |
| `unit` | string/null | opt | e.g. `"%"`, `"fold"`, `"pmol/min"`. |
| `source` | object | **yes** | `E025` when not an object. |

### `evidence[].source`

| Key | Type | Req | Rule |
|---|---|---|---|
| `file` | string | **yes** | `E026`; must name a real file in the material set. |
| `locator` | string | **yes** | `E026`; a resolvable span. Text: `"L21"` / `"L21-L22"`. CSV/TSV: `"row 5"` (header is row 1). |
| `quote` | string | **yes** | `E027`; must be a **verbatim** substring of the material. A paraphrase fails `E905`. |

For a table row the safest quote is the raw row line, byte-for-byte, e.g.
`"hypoxia,1,24,82.1,2.74,12.6,0.19"` with `"locator": "row 5"`.

---

## 6. `panels[]`

At least one panel is required. A panel is the drawing container; a data figure typically has one
panel per sub-figure (a/b), each with one chart and its own legend.

| Key | Type | Req | Rule |
|---|---|---|---|
| `id` | string | **yes** | `E032`; unique (`E033`). |
| `role` | string | **yes** | One of `problem`, `method`, `results`, `conclusion` (`E034`). A data chart uses `results` (or `method` for a methods figure). |
| `title` | string | **yes** | Non-empty (`E035`); drawn as the panel title. |
| `charts` | array | *rec* | The data. Validated whenever present. |
| `legend` | object | opt | `E058`/`E059` when malformed. |
| `colorbars` | array | opt | For heatmaps/continuous encodings. |
| `nodes` / `edges` | array | opt | Legacy schematic fields; ignored by a data chart. Do not add them just to satisfy the four-role warn. |

For compatibility the validator still emits **`W070`** warns for absent `problem`/`method`/
`conclusion` roles. For a data chart these are **cosmetic warnings, not failures** — the four-role
reading order belongs to the sibling graphical-abstract Skill. Do not fabricate panels to silence
them.

---

## 7. `panels[].charts[]` — the data

| Key | Type | Req | Rule |
|---|---|---|---|
| `id` | string | rec | Stable chart id (`C1`, `C2`), used in mark ids and the SVG. |
| `type` | string | **yes** | `bar`, `barh`, `line`, `scatter`, `dot`, `heatmap`, `box`, `violin`, `waterfall`, `none` (`E050`). |
| `title` | string | opt | Chart title. |
| `xlabel` | string | rec | x-axis label; gate K checks it exists. |
| `ylabel` | string | rec | y-axis label; gate K checks it exists. |
| `unit` | string | **rec** | y-axis unit (`W057`); a quantitative axis with no unit is **`E914`** at gate K. |
| `x_unit` | string | rec | x-axis unit (`E914` applies when the x axis is quantitative). |
| `categories` | array<string> | req for bar/barh | Category order; each label must occur **verbatim** in the material (gate J). |
| `baseline` | number | rec | Bar/area baseline; must be `0` or carry `justification` (**`E915`**). |
| `axis` | object | opt | `{"x": {...}, "y": {...}}` — see below. Missing axes are derived from the data. |
| `series` | array | **yes** | Non-empty (`E051`). |
| `significance` | array | opt | Brackets between categories — see below. |
| `colorbar` | object | opt | Heatmap colour scale (`vmin`/`vmax`/`cmap`/`unit`); gate D compares the range with the data. |

### `panels[].charts[].axis`

```jsonc
"axis": {
  "x": {"label": "time", "unit": "h", "scale": "linear", "tick_start": 0, "tick_end": 24, "tick_step": 6},
  "y": {"label": "mitoSOX signal", "unit": "fold", "scale": "log10", "tick_start": 1, "tick_end": 4, "tick_step": 1}
}
```

* `scale` ∈ `linear` \| `log10` (a `log10` axis with non-positive data or an inverted range is an
  illegal spec → exit `4`).
* `tick_start`/`tick_end`/`tick_step` are advisory but read: a range that clips data points or
  leaves them off-page is a gate-A problem; a range far wider/narrower than the data triggers
  **`W920 W_AXIS_TICK_RANGE_SHORT`**.
* A quantitative axis without `unit` → **`E914`** (gate K). Categorical axes (bar categories, box
  groupings) do not need a unit.

### `panels[].charts[].series[]`

| Key | Type | Req | Rule |
|---|---|---|---|
| `name` | string | **yes** | `E053`. For a grouped bar this is the condition/category label; it must occur verbatim in the material. |
| `values` | array<number> | **yes** | `E054`/`E055`. Binds to a real data column/cell via gate J. |
| `column` | string | rec | The source column this series was read from; also what `locate --cell` echoes back. |
| `x` | array<number> | rec (see below) | Per-point x coordinates (one per value). |
| `categories` | array<string> | opt | Per-series category labels when a chart has several series and one category axis. |
| `errors` | array<number> | opt | Per-point uncertainty. Must be the same length as `values`. |
| `error_kind` | string | **req when `errors` present** | `sd` \| `sem` \| `ci95` \| `iqr` \| `range`; missing meaning is **`E911`**. |
| `n` | int \| array<int> | **req when the material has replicate rows** | Sample size per point; missing when replicates exist is **`E913`**. |
| `color` | string | opt | Hex colour; defaults to the palette order. |
| `source` | string | **req for gate E** | One evidence id (`W056`, then `E903`). |
| `sources` | array<string> | alternative | Several evidence ids, for a series spanning many rows. |
| `derived` | bool | opt | `true` when the number is computed (a mean, a ratio, a normalisation). |
| `source_values` | array<number> | **req when `derived`** | The exact inputs; without them a derived value is not value-checked (`E907` warn) and gate J cannot confirm it. |

Positional semantics: `len(values)` must equal `len(categories)` for a category chart and
`len(x)` for a line/scatter. `errors`, `n` (when an array) and `source_values` must line up with
`values` positionally.

**x-axis priority (frozen):** `series[].x` wins when present; otherwise the chart-level fallback
`chart.x = {"label": "time (h)", "values": [0,6,12,24]}` is used; if neither exists the index
`0..n-1` is used. Prefer `series[].x` — it is per-series and survives a series being added or
reordered. A line chart whose points come from different time columns should declare
`chart.x.label`/`x_unit` for the axis text and `series[].x` for the coordinates.

### `panels[].charts[].significance[]`

```jsonc
"significance": [{"between": ["control", "hypoxia"], "label": "**", "test": "Welch t-test", "p": 0.003}]
```

* `between` names two categories/series labels that must exist in the chart.
* `label` is what gets drawn (`*`, `**`, `***`, `n.s.`, or a short phrase).
* `test` + `p` must be supplied, **or** `defined_in_caption: true` when the caption defines them;
  otherwise **`E912`** (gate K). This Skill never computes the p-value — it renders the one the
  material states.

---

## 8. Derived values, in one paragraph

A bar of a group mean is the normal case: the cell contains three replicate values, the drawn
value is their mean. Declare `derived: true` and list the three cells' values in
`source_values`; gate J then checks that those inputs exist in the mapped cells and that the
arithmetic is reproducible. If you instead want each replicate on the figure, use `scatter`/`dot`
with one mark per cell and `n` on the series. What you must **not** do is print a mean with no
declared inputs — that is `E910`/`E907` territory, and the point of the Skill is that the reader
can get from the drawn bar back to the three numbers behind it.

---

## 9. Code table

Spec-structure codes (from `scene.validate_spec`, next to `spec-issues.json`):

| Code | Severity | Meaning |
|---|---|---|
| `E001`–`E007` | error | meta / canvas missing or out of range |
| `E011` | error | unknown palette id without `colors[]` |
| `E020`–`E027` | error | evidence list / item / source / quote problems |
| `E030`–`E063` | error | panel, node, edge, chart, legend, colorbar structure |
| `W010`, `W012`, `W028`, `E041`, `W042`, `W048`, `W056`, `W057`, `W064`, `W070` | warn | advisory (palette default, language, unknown node shape → `rrect`, node/edge/series have no source, chart has no unit, missing four-role coverage) |

Gate codes (frozen, `CONTRACT.md` §7) — never reused for another meaning:

| Code | Gate | Meaning |
|---|---|---|
| `E903` | E | element has no evidence `source` |
| `E904` | E | citation id does not exist in `evidence[]` |
| `E905` | E | quote does not resolve verbatim in the material |
| `E906` | E | number not present in its cited quote |
| `E907` | E | derived value without recomputable `source_values` |
| `E910` | **J** | drawn mark not locatable in the data (or value mismatch > `1e-9`) |
| `E911` | **K** | error bars drawn without a declared `error_kind` |
| `E912` | **K** | significance drawn without `test`+`p` or caption definition |
| `E913` | **K** | replicate rows exist but no `n` and no caption statement |
| `E914` | **K** | quantitative axis without a `unit` |
| `E915` | **K** | bar/area baseline not zero and no `justification` |
| `E916` | build/CLI | unknown preset id (exit `4`) |
| `E917` | **L** | CJK label whose glyph is missing from the resolved font |
| `W920` | K | axis tick range too short for the data (emitted) |
| `W921` | D/K | legend without matching series — contract-frozen id; on the current revision gate D reports the condition in its own `detail` |
| `W922` | K | caption contradicts the drawn figure or repeats the case's stale caption (emitted) |
| `W923` | K/A | overlap risk between annotations — contract-frozen id; the layout reports collision risk in its scene warnings |

---

## 10. Minimal valid data-chart spec (copy-paste)

This is the shape used by `examples/synthetic-001`; it validates with zero errors. Each bar is the
mean of three replicate rows, so it is `derived` and cites **all three** rows via `sources` — gate
E verifies the `source_values` in the cited quotes, gate J verifies the arithmetic and the mapping.

```json
{
  "meta": {
    "case_id": "SYNTHETIC-CHART-001",
    "language": "en",
    "preset": "nature-single",
    "figure_id": "figure03",
    "title": "HIF-1alpha knockdown partly rescues viability under hypoxia",
    "caption": "Cell viability (mean +/- SD, n = 3) after 24 h in control and hypoxia.",
    "canvas": {"w_mm": 120, "h_mm": 90, "dpi": 300}
  },
  "palette": {"id": "okabe-ito"},
  "texts": {"caption": "Cell viability (mean +/- SD, n = 3) after 24 h in control and hypoxia.", "title": "HIF-1alpha knockdown partly rescues viability under hypoxia"},
  "evidence": [
    {"id": "E1", "kind": "number", "claim": "control viability replicate 1", "value": 99.2, "unit": "%",
     "source": {"file": "materials/experiment.csv", "locator": "row 2", "quote": "control,1,24,99.2,1.02,18.6,0.42"}},
    {"id": "E2", "kind": "number", "claim": "control viability replicate 2", "value": 98.4, "unit": "%",
     "source": {"file": "materials/experiment.csv", "locator": "row 3", "quote": "control,2,24,98.4,0.97,18.9,0.41"}},
    {"id": "E3", "kind": "number", "claim": "control viability replicate 3", "value": 99.7, "unit": "%",
     "source": {"file": "materials/experiment.csv", "locator": "row 4", "quote": "control,3,24,99.7,1.01,18.3,0.42"}},
    {"id": "E4", "kind": "number", "claim": "hypoxia viability replicate 1", "value": 82.1, "unit": "%",
     "source": {"file": "materials/experiment.csv", "locator": "row 5", "quote": "hypoxia,1,24,82.1,2.74,12.6,0.19"}},
    {"id": "E5", "kind": "number", "claim": "hypoxia viability replicate 2", "value": 84.3, "unit": "%",
     "source": {"file": "materials/experiment.csv", "locator": "row 6", "quote": "hypoxia,2,24,84.3,2.88,12.1,0.20"}},
    {"id": "E6", "kind": "number", "claim": "hypoxia viability replicate 3", "value": 83.0, "unit": "%",
     "source": {"file": "materials/experiment.csv", "locator": "row 7", "quote": "hypoxia,3,24,83.0,2.81,12.4,0.19"}},
    {"id": "E7", "kind": "condition", "claim": "viability measured at 24 h with three biological replicates",
     "source": {"file": "materials/methods.md", "locator": "L9",
                "quote": "Viability was measured at 24 h with three biological replicates per condition."}}
  ],
  "panels": [
    {"id": "P1", "role": "results", "title": "Cell viability",
     "charts": [
       {"id": "C1", "type": "bar", "xlabel": "condition", "ylabel": "cell viability", "unit": "%",
        "categories": ["control", "hypoxia"],
        "baseline": 0,
        "axis": {"y": {"label": "cell viability", "unit": "%", "scale": "linear",
                       "tick_start": 0, "tick_end": 100, "tick_step": 20}},
        "series": [
          {"name": "control", "column": "cell_viability_pct", "values": [99.1],
           "errors": [0.66], "error_kind": "sd", "n": 3,
           "derived": true, "source_values": [99.2, 98.4, 99.7],
           "sources": ["E1", "E2", "E3"]},
          {"name": "hypoxia", "column": "cell_viability_pct", "values": [83.13],
           "errors": [1.11], "error_kind": "sd", "n": 3,
           "derived": true, "source_values": [82.1, 84.3, 83.0],
           "sources": ["E4", "E5", "E6"]}
        ],
        "significance": [{"between": ["control", "hypoxia"], "label": "**",
                          "test": "Welch t-test", "p": 0.003}]}
     ],
     "legend": {"title": "condition",
                "items": [{"label": "control"}, {"label": "hypoxia"}]}}
  ],
  "uncertainty": [],
  "limitations": []
}
```

The shipped `examples/synthetic-001/expected/spec.json` is the full version: three conditions,
two panels (a grouped bar and a line chart), and one evidence item per source row.

---

## 11. What `validate_spec` does **not** check

Structural validation only. It does not open the materials, measure text, check colours, verify
values against cells, or inspect the SVG. Those are gates A–L in `chart.py check`. Passing
`validate_spec` means "this spec is well-formed", **never** "this figure is correct".

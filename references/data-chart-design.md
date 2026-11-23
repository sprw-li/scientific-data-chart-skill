# Choosing the chart, and the annotation set gate K demands

This page is the link between **the shape of the table** and **the spec you author**. It is
deliberately about data charts (not schematics) and it never tells you what the data means — that
comes from the material.

---

## 1. Read the table first

Before choosing a chart, answer these six questions from `work/data-tables.json` (or the raw
file), not from the filename:

1. **What is one row?** A replicate animal/culture? A time point? A summary row that must *not* be
   counted as a replicate?
2. **What is the grouping column?** Its values become `categories` (verbatim).
3. **What is the measured column?** Its header usually gives the quantity; the unit may live in
   the header, in a measurement note, or in the caption — find it, do not assume it.
4. **Are there replicate rows per group?** If yes, the figure needs `n` (`E913`) and the
   statistic you draw (mean, median) must be declared `derived` with `source_values`.
5. **Is there an uncertainty column, or a note stating the spread?** If the figure shows error
   bars, `error_kind` is mandatory (`E911`).
6. **Does the material report a comparison or a significance statement?** If yes it can become a
   significance bracket — with the `test`+`p` the material gives, or `defined_in_caption` if the
   caption defines it (`E912`). If the material reports a comparison but no p-value, **draw the
   comparison without a p-value**; do not invent one.

---

## 2. Chart type by data shape

| Data shape | Chart | Spec essentials |
|---|---|---|
| Category × one measure, 1 value per category | `bar` (or `barh` for long labels) | `categories`, one series whose `name` matches the category, `baseline: 0`, y `unit` |
| Category × group × replicate → mean ± spread | grouped `bar` + error bars | one series per group, `errors` + `error_kind`, `n`, `derived: true` + `source_values` |
| Continuous x (time/dose) × one or more series | `line` | `series[].x`, point markers, x `unit` (e.g. `h`), optional error bars |
| Two continuous variables, one point per observation | `scatter` | `series[].x`, `x_unit`, no `baseline` |
| Points with a category axis, few of them | `dot` | `categories`; good when bars would exaggerate |
| Distribution comparison across groups | `box` | five-number summaries must come from the real values, not estimated |
| Distribution shape / density | `violin` | KDE computed from the real values |
| Category × category, one numeric value per cell | `heatmap` | `colorbar{vmin,vmax,cmap,unit}` matching the data range (`gate D`) |
| Contributions to a total (waterfall of deltas) | `waterfall` | ordered contributors, a stated start/end total |
| Comparison of paired conditions | `line` with paired `x`, or `dot` | label both members of each pair |

Rules that survive every choice:

* Do **not** use a bar chart for a mean of fewer than 3 replicates without saying so; if `n < 3`,
  say it in the caption and keep the individual points visible (`dot`/`scatter`).
* Do **not** truncate a bar baseline (`E915`). Bars start at zero, always; a non-zero baseline
  needs an explicit `justification` and is what the gate is there to catch.
* Do **not** put a categorical series on a log axis; a log axis with non-positive data is an
  illegal spec (exit `4`).
* With ≤3 categories prefer position over colour; with 4+ use the CVD-safe palette order and
  check gate C.

---

## 3. The annotation set gate K checks

| Element | Spec field(s) | Gate K code when missing/wrong |
|---|---|---|
| x-axis label and y-axis label | `charts[].xlabel`, `charts[].ylabel` | reported as an annotation gap |
| Quantitative axis unit | `charts[].unit` (y), `charts[].x_unit` (x) | **`E914`** |
| Bar/area zero baseline | `charts[].baseline: 0` (+ `justification` if not) | **`E915`** |
| What the error bars mean | `series[].error_kind` ∈ `sd`/`sem`/`ci95`/`iqr`/`range` | **`E911`** |
| Significance statement | `charts[].significance[]` with `between`+`label`+`test`+`p`, or `defined_in_caption` | **`E912`** |
| Sample size | `series[].n` (int or per-point) | **`E913`** when the material has replicate rows |
| Caption consistent with the drawn figure | `meta.caption` / `texts.caption` | warn **`W922`** |
| Tick range wide enough for the data | `axis.{x,y}.tick_start/tick_end/tick_step` | warn **`W920`** |
| No annotation collisions | layout is automatic | warn **`W923`** |
| Legend matches the drawn series | `panels[].legend.items` | warn **`W921`** |

**Every one of these must be readable off the material, not off the draft figure.** The bundled
test fixtures include a case whose `figure.stale_caption` names the wrong unit, the wrong statistic
and the wrong number of groups: `case.json` marks such a caption as **metadata, not source
data**, and the authoritative statement is the measurement note + the table. That trap is the
single most common way to fail gate K.

---

## 4. What "described honestly" looks like

A good authored spec reads like a careful methods paragraph, expressed as fields:

* `categories: ["control", "hypoxia", "hypoxia+siHIF1A"]` — exactly the strings in the table.
* `unit: "%"`, `x_unit` absent because x is categorical.
* `series[].n: 3` and a caption that says `mean ± SD, n = 3`.
* `error_kind: "sd"` — not "error bars", not "±" with no meaning.
* `significance: [{"between": ["control","hypoxia"], "label": "**", "test": "Welch t-test",
  "p": 0.003}]` — the test and p exactly as the material reports them.
* `derived: true` + `source_values: [99.2, 98.4, 99.7]` on every mean, so a reviewer can rebuild
  the bar by hand.

A bad spec is complete-looking but hollow: a bar labelled "fold change" with no unit, error bars
with no meaning, a significance star with no test, four categories one of which is not in the
table, or a mean with no inputs. Each of those is a specific, named gate failure — which is the
point: the gate list is a checklist for authoring, not just a rejection mechanism.

---

## 5. Multi-panel figures

* One panel per sub-figure; panel `title` is the sub-figure title, and the layout adds the `a`/`b`
  labels.
* Keep the same y-scale across panels that will be compared; if you must differ, say so in the
  caption.
* Each panel gets its own chart and legend; a legend shared across panels is not currently
  supported by the layout.
* Panel order in `panels[]` is the reading order.

---

## 6. When the data does not fit any chart

Some materials are not chartable: a single number, a list of names, a qualitative statement. Do
not force a bar chart out of one data point. Options, in order of preference:

1. Render a simple annotated figure (`type: "none"` with `texts`) and state in `limitations` that
   the material does not support a quantitative chart.
2. Record the gap in `uncertainty` and ship `status: "partial"`.
3. If the material is a mechanism/pathway description rather than data, say plainly that this
   Skill is the data-chart Skill and that a schematic is out of its scope.

Never manufacture categories, replicates or units to make the figure look complete.

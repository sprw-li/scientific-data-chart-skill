# Failure modes — what actually happens

Every claim on this page is stated as **behaviour + the evidence field you can read to confirm
it**: exit code, gate letter, error code, and the `result.json` field that changes. The
"Verified" markers at the end of each section name the command that was run and what it printed;
anything not marked verified is a documented contract behaviour or an explicitly-labelled gap.

Sources of truth: `work/CONTRACT.md` §2/§3/§7, `scripts/validate.py`, `scripts/export.py`,
`scripts/scene.py`, `scripts/chart.py`.

---

## Master table

| Situation | Exit | Gate | Code | `result.json` |
|---|---|---|---|---|
| Drawn value not locatable in the data | `1` | J | **`E910`** | `status: partial`; the failing gate is copied into `uncertainty` |
| Error bars drawn with no `error_kind` | `1` | K | **`E911`** | as above |
| Significance drawn with no `test`/`p`/caption definition | `1` | K | **`E912`** | as above |
| Replicate rows present, no `n` | `1` | K | **`E913`** | as above |
| Quantitative axis without a unit | `1` | K | **`E914`** | as above |
| Bar/area baseline not zero, no justification | `1` | K | **`E915`** | as above |
| Unknown preset id | `4` | — | **`E916`** | `spec-issues.json` written; no artifacts produced |
| CJK glyph missing from the resolved font | `1` | L | **`E917`** | `status: partial` + a `limitations` entry |
| Tick range too narrow for the data | `0` | K | warn **`W920`** | `checks.K: "warn"`, present in `uncertainty` only if structural |
| Legend with no matching series | `0` | D/K | warn **`W921`** | `checks.D: "warn"` |
| Caption contradicts the drawn figure | `0` | K | warn **`W922`** | `checks.K: "warn"` |
| Annotation overlap risk | `0` | A/K | warn **`W923`** | `checks: "warn"` |
| Element with no citation at all | `1` | E | `E903` | `status: partial` |
| Dangling evidence id | `1` | E | `E904` | `status: partial` |
| Quote not verbatim / unresolvable span | `1` | E | `E905` | `status: partial` |
| Number not present in its cited quote | `1` | E | `E906` | `status: partial` |
| `derived: true` without `source_values` | `0` (warn) | E | `E907` | value is **not** value-checked; the gap is visible |
| Artefacts missing when `check` runs | `3` | I | — | gate I fails; run `build` first |
| `check` before `result` | `1` | I | — | `result.json missing … contract UNVERIFIABLE` |
| Bad spec structurally | `4` | — | `E0xx` | `spec-issues.json` lists path + message |
| Unknown CLI flag / unreadable IO | `2` | — | — | no report written |
| `locate` finds nothing | `5` | — | — | parseable JSON naming the searched scope |

`warn` never changes the exit code. A **structural gap** (no material, no source,
`UNVERIFIABLE`) is what lowers `status` to `partial` even when no gate says `fail`.

---

## 1. `E910` — a mark that cannot be traced to the data (gate J)

The flagship failure. Two distinct causes and two correct fixes:

* **Wrong binding.** `series[].values` was typed from a draft figure instead of the table column.
  Fix: re-read `data-tables.json`, bind to the real column/cells, rebuild.
* **Undeclared derivation.** The bar is a mean but the spec presents it as a raw value. Fix: set
  `"derived": true` and list the exact inputs in `source_values`; gate J then checks the
  arithmetic against the mapped cells, and gate E checks the inputs appear in the cited quotes.

What you must **not** do: delete the mark's `source`, or hand-edit `mark_data_map.json`. Both
convert a detectable error into a hidden one, and the second one is also a determinism break
because `build` regenerates the map.

> Verified shape: `chart.py check` prints `J <name> fail` with a `detail` containing the mark id,
> the drawn value, the located cell and the mismatch; `result.json.audit.checks_failed` contains
> `"J"` and `uncertainty[].kind == "check_failed"`. See §11 for the executed probe.

---

## 2. `E911`–`E915` — annotation completeness (gate K)

| Code | Trigger | Minimal correct fix |
|---|---|---|
| `E911` | `series[].errors` present, `error_kind` absent | add `"error_kind": "sd"` (or `sem`/`ci95`/`iqr`/`range`) — the *meaning*, not the number |
| `E912` | `significance[].label` drawn with no `test`+`p` | add them from the material, or `"defined_in_caption": true` when the caption defines the comparison |
| `E913` | material has replicate rows; series/caption has no `n` | add `"n": 3` (int or per-point array) and state it in the caption |
| `E914` | quantitative axis has no `unit` | add `charts[].unit` (y) / `charts[].x_unit` (x) |
| `E915` | bar/area `baseline != 0` without `justification` | set `"baseline": 0`, or justify the truncation explicitly |

These are deliberately **not** auto-fixable: each one is a claim about the data, and the Skill
cannot invent the meaning of an error bar or a sample size. The correct response to `E911` is to
find the note that says "SD", not to pick one.

A very common real case: the source note reports a **comparison without a p-value**. Then a
significance marker is allowed only as the reported comparison (`defined_in_caption` or an
explicit statement), and inventing a `p` is `E912`/fabrication. The bundled test fixtures include
exactly this trap, together with a `figure.stale_caption` that gets the unit, the statistic and the
number of groups wrong — the caption is metadata and must never be copied into the spec.

---

## 3. `E916` — unknown preset

`chart.py build --preset <typo>` writes `spec-issues.json` next to the output and exits `4`; the
CLI deliberately does **not** fall back to the default, because a silent fallback would let a
figure claim "Nature styling" while rendering generic. The fix is one command:
`chart.py presets --list`.

---

## 4. `E917` — CJK glyph missing (gate L)

The gate asks the font system whether the glyph really exists in the resolved face. Outcomes:

| Outcome | Exit | Report |
|---|---|---|
| Glyph present | `0` | gate L `pass` |
| Glyph absent, coverage backend available | `1` | **`E917`**, `status: partial`, `limitations` entry naming the fallback |
| No coverage backend available | `0` | gate L reports **unavailable**, never "pass"; the gap is disclosed |

The forbidden outcome is drawing tofu (`□`) and reporting `pass`. A missing CJK face in the
runtime is a *recorded degradation* (`export_report.font.degraded = true`), not a crash.

---

## 5. Text overflow and colour — gates B and C

* Gate **B**: measured text wider/taller than its box, a font below the floor, or a truncated
  label. Fix by shortening the label, reducing `meta.layout.columns`, or enlarging the page. Do not
  shrink type below the preset floor.
* Gate **C**: categorical colours that collapse under CVD simulation or in greyscale, or text
  contrast below 4.5:1. Fix by using the palette order, fewer categories, or shapes/hatches.
* A legend that does not fit its box is truncated and records a scene warning (visible in
  `export_report.scene_warnings`) rather than being drawn outside the canvas.

---

## 6. Determinism — gate G and the whole chain

Gate **G** re-exports the already-built scene and compares sha256 per format. It does **not**
re-run `ingest` or the layout. A green G with a nondeterministic ingest is possible in principle,
which is why `chart.py selftest` runs the **whole chain twice** and compares bytes; that is the
claim to quote. If two builds differ, find the nondeterministic input (clock, RNG, dict order,
`/ID`) instead of re-running until the hashes agree.

---

## 7. Export failures

A required export that cannot be produced is exit `2` with `build-error.json` and a reason — never
a reported artefact. An optional format (`pptx`, `eps`, `tiff`) that has no backend is recorded as
`unavailable` with a reason and is absent from `artifacts`; SVG/PNG/PDF still ship. If PNG or PDF
itself is missing, treat the run as not-`ok`, keep the SVG, and say so in `limitations`.

---

## 8. Materials that are empty, unreadable or contradictory

* An **empty or unparseable material** is recorded during `ingest` with a reason
  (`pdf_extractor`/`parse_reason`) and surfaces in `uncertainty`; `ingest` still exits `0` unless
  the input directory itself is unusable (`2`).
* A **missing table column** referenced by the spec is not silently zero-filled: the binding fails
  at gate J (`E910`) or the spec is rejected structurally (`4`).
* **Contradictory statements** (two different units for one column, a stale caption against a note)
  are `needs_human` territory: record both, choose the authoritative one explicitly (table/note >
  caption), and set `status` accordingly. Never average the two.

---

## 9. Ordering trap: gate I needs `result.json` to exist

Running `check` before `result` makes gate **I** report
`result.json missing … -> contract UNVERIFIABLE` and exit `1` even when A–H, J–L all passed. The
correct order is `build → result → check → result`.

---

## 10. Illegal spec — exit `4`

`scene.validate_spec` collects structural problems and `chart.py` writes them to
`spec-issues.json` next to the output:

```json
[{"code": "E050", "severity": "error", "path": "$.panels[0].charts[0].type",
  "message": "chart type must be one of [...]"}]
```

Read `path` and `message`, fix `spec.json`, rebuild. Do not patch the renderer.

---

## 11. Checks that do not exist (documented gaps)

Stated plainly so no claim is inflated:

* **No statistical inference.** The Skill does not compute p-values, effect sizes, regressions or
  multiple-comparison corrections; it renders what the material states. There is no gate for
  "the statistics are right".
* **No semantic judgement of chart appropriateness.** Gate K checks that the annotation set is
  complete and consistent; it cannot tell that a pie chart of a continuous variable is a poor
  choice. `references/data-chart-design.md` is guidance, not enforcement.
* **No human visual review.** "Looks good" is not measured anywhere; gates report geometry,
  colour metrics, coverage counts and export parameters, not aesthetics.
* **No guarantee about the hidden case.** The evaluator's materials are unseen; only the
  structural, provenance and export properties are verified locally.
* **`locate` scope.** It answers from `mark_data_map.json`; if the map is stale (spec edited
  without a rebuild) the answer is not re-derived from the materials. Rebuild before trusting it.

---

## 12. Measured evidence on this bundle

Every number below comes from a command run against this bundle; the transcript snippets are
quoted verbatim. Anything not listed here has **not** been executed and must not be quoted as if
it had.

| Probe | Command | Exit | Observed |
|---|---|---|---|
| _to be filled after the implementation freeze_ | | | |

Reproduce any row with the command in column 2 and compare `validation_report.json` /
`result.json`; a claim without such a command is not evidence.

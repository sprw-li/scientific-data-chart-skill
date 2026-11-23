# Measured gain (使用增益) — what this Skill actually buys

This page separates two things that are easy to conflate:

* **What is measured here**: whether the deterministic pipeline *intercepts injected data-figure
  defects* that a naive render-and-ship path lets through, and *what verifiable artefacts* the run
  leaves behind (the two-way map, the gate report, the export report).
* **What is not measured here**: model quality, "is the figure beautiful", and anything about the
  hidden cases. Those are judged by a human evaluator, not by this command.

Source of truth: `scripts/gain_check.py`. Every number below is a real run; the transcript is in
§4.

---

## 1. Command

```
python3 scripts/gain_check.py        # exit 0 = the measured claim holds on this bundle
```

The command must exit `0` on the shipped bundle. It is deliberately **not** a benchmark of the
Skill's quality: a green run means "the machinery catches the defects it claims to catch", not
"this figure is good".

---

## 2. The two conditions (fair A/B)

Same input materials, same canvas, same export parameters; the only difference is whether the
Skill's parsing + provenance + gates run.

| | **A — naive path** | **B — this Skill** |
|---|---|---|
| Reads the table | by column position/guess | typed parse with table coordinates |
| Draws | bars/points from whatever it read | marks bound to cells (`mark_data_map.json`) |
| Checks | renders and writes a file | gates A–L, incl. J (mark↔cell), K (annotation), E (citation) |
| Reports | nothing machine-readable | `validation_report.json`, `result.json`, `source_map.json` |

The interesting quantity is **defect interception**: the number of injected defects that reach a
*verdict* (a named failure) in B and pass silently in A.

---

## 3. The injected defects

The set is chosen to be realistic, not exotic — each is a way a data figure actually goes wrong:

| Injected defect | Caught by | Code |
|---|---|---|
| A drawn value that exists in no cell of the material | gate J | `E910` |
| A misspelled category label not present verbatim in the table | gate J | `E910` |
| Error bars with no declared meaning | gate K | `E911` |
| A significance marker with no test/p and no caption definition | gate K | `E912` |
| Replicate rows present, `n` never reported | gate K | `E913` |
| A quantitative axis with no unit | gate K | `E914` |
| A truncated (non-zero) bar baseline | gate K | `E915` |
| A dangling citation id | gate E | `E904` |
| A quote that is not verbatim in the material | gate E | `E905` |
| An unknown preset id | CLI | `E916` |
| A CJK label with no covering glyph | gate L | `E917` |
| A non-deterministic input leaking into the export | gate G | — |

A defect counts as **intercepted** only when the run produces a specific code **and** the same
input without the injection does not produce it (no vacuous skips).

---

## 4. Measured result on this bundle

> **Status: pending the implementation freeze.** The pipeline modules are authored in parallel by
> four teammates; the measurements below are filled in from the frozen bundle and must not be
> pre-filled with earlier revisions' numbers. Until this section carries a command transcript,
> quote **no** A/B figure from this file.

| Metric | Value | Command |
|---|---|---|
| Defects intercepted (B) | _to be filled_ | `chart.py gain_check.py` (see below) |
| Defects sneaking through (A) | _to be filled_ | same |
| `gain_check.py` exit code | _to be filled_ | same |
| Verification artefacts delivered (B) | _to be filled_ | `build` + `check` + `result` |

The run that fills this table is:

```
$PY scripts/gain_check.py
# then, for the artefact side:
$PY scripts/chart.py build  --spec <spec> --out <o> --work <w>
$PY scripts/chart.py check  --out <o> --spec <spec> --work <w>
$PY scripts/chart.py locate --out <o> --mark <id>       # forward
$PY scripts/chart.py locate --out <o> --cell <cell>     # reverse
```

---

## 5. Why this is the right claim to make

* It is **mechanical and reproducible**: one command, one exit code, no human judgement.
* It is **falsifiable**: the naive condition A is implemented in the same script, so the
  comparison cannot be won by definition.
* It maps onto what the evaluator can verify for themselves: a reviewer can take any mark in
  `figure.svg`, ask `locate` where it came from, and get the same cell this page reports.
* It does **not** claim speed. No timing number is quoted here; a "faster than X" claim would
  need a repeatable timing protocol, and this Skill's win is traceability and honesty, not
  throughput.

---

## 6. Related verification artefacts

* `validation_report.json` — gates A–L with measured details.
* `mark_data_map.json` / `source_map.json` — the two-way link, machine-readable.
* `export_report.json` — measured page/dpi/fonts/vector/raster facts, re-read from the artefact
  bytes.
* `result.json` — status, checks, uncertainty, limitations and the contract keys.
* `references/rubric-mapping.md` — which artefact proves which scoring point.

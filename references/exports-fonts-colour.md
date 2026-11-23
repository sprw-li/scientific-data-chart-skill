# Exports, fonts and colour

Sources of truth: `work/CONTRACT.md` §2/§7/§8/§9, `scripts/export.py`, `scripts/fonts.py`,
`scripts/measure.py`, `scripts/scene.py`. Measured numbers quoted below were read from the
pipeline's own `export_report.json`, which is produced by **re-reading the artefact bytes**, not by
trusting a backend's self-report.

---

## 1. Canvas and units

| Property | Value | Source |
|---|---|---|
| Scene coordinate space | millimetres, origin top-left, +y down | `scene.py` |
| Font sizes / line widths | points | `scene.py` |
| Conversion | `pt = mm × 72/25.4` (×2.834645), `mm = pt × 25.4/72` | `scene.mm_to_pt` / `pt_to_mm` |
| Default page | **180 × 120 mm** | `scene.DEFAULT_CANVAS` |
| Default margin / dpi | 3 mm / 300 | `scene.DEFAULT_CANVAS` |
| Allowed `w_mm`, `h_mm` | 20–1000 | `E003`, `E004` |
| Allowed `dpi` | 72–1200 | `E005` |
| Allowed `margin_mm` | 0–20 and `2×margin < min(w,h)` | `E006`, `E007` |

Resolution order for the page box: scene canvas → `spec.meta.canvas` → preset defaults →
`scene.DEFAULT_CANVAS`. Non-numeric values are ignored, not coerced. A preset supplies the width
and a working height; an explicit `meta.canvas` always wins.

---

## 2. Export parameters, and what is actually measured

| Parameter | Contract | How it is proven |
|---|---|---|
| Formats | `svg,png,pdf` by default (`--formats`) | `export_report.formats` |
| DPI | `--dpi`, default 300 | `export_report.dpi`, PNG `pHYs` |
| Page | preset / `meta.canvas` | `export_report.page_mm` vs requested |
| Colour space | sRGB (declared) | `export_report.colorspace` |
| PNG | lossless raster, pixel size == page × dpi | parsed from the PNG bytes |
| PDF | 1 page, vector, fonts embedded | parsed from the PDF bytes |
| SVG | layered named groups, real `<text>` | counted in the SVG text |

`export.py` re-reads what it wrote (standard library only):

* **PNG** — signature, `IHDR` pixel size/bit depth, `pHYs` → dpi (metre units only), plus a
  `tEXt`/`iTXt` `dpi=` fallback. `expected_px = round(w_mm/25.4×dpi) × round(h_mm/25.4×dpi)`, so
  gate H can compare expected vs actual without an image library.
* **PDF** — `%PDF` header, page count (`/Type /Page`, falling back to `/Count`), `MediaBox` in
  points, font embedding (`/FontFile`, `/FontFile2`, `/FontFile3`), vector text (`Tj`/`TJ`
  counts), accidental rasterisation (`/Subtype /Image`). Flate streams are decoded with `zlib`
  before searching, so a compressed content stream is still inspected.
* **SVG** — `<text>` count, `<g>` count, named-group count, `<image>` count, byte size.

For the **default 180 × 120 mm page at 300 dpi** the arithmetic values are
**2126 × 1417 px** and a MediaBox of **510.236 × 340.157 pt**. The shipped
`examples/synthetic-001` uses a different page; the authoritative numbers for any run are the ones
in its own `export_report.json`, and gate H compares them.

### Optional formats — available or honestly unavailable

`figure.pptx` (real presentation), `figure.eps`, `figure.tiff` and `preview/figure.png` are
**optional**. When a backend exists they are produced; when it does not, the export report records
`available: false` with a reason (e.g. `python-pptx not installed`), the file is **not** listed in
`result.json.artifacts`, and the gap flows into `limitations`. A missing backend never fakes a
file and never fails the run: SVG/PNG/PDF are the required set.

---

## 3. Fonts

`fonts.resolve_fonts(lang)` never raises and never returns `None`; it walks explicit, ordered
candidate lists (no directory-scan order, no RNG, no time), so two runs on the same machine pick
the same face.

* **Latin**: `arial.ttf` → `calibri.ttf` → `segoeui.ttf` → `DejaVuSans.ttf` → `liberationsans.ttf`
  → `freesans.ttf` (bold partners looked up separately, falling back to the regular face).
* **CJK**: `simhei.ttf` → `msyh.ttc` → `msyh.ttf` → `NotoSansSC-VF.ttf` →
  `NotoSansCJKsc-Regular.otf` → `simsun.ttc` → `NotoSansJP-VF.ttf` → `NotoSansKR-VF.ttf`
  (plain `.ttf` before `.ttc` collections).
* Font directories: `%WINDIR%\Fonts`, `%LOCALAPPDATA%\Microsoft\Windows\Fonts`, `~/.fonts`,
  `/usr/share/fonts`, `/Library/Fonts`, and matplotlib's bundled `mpl-data/fonts/ttf`
  (DejaVu Sans, the guaranteed last resort).

CJK is requested for `lang` in `zh/cn/chinese/ja/jp/japanese/ko/kr/korean`, for anything
containing `han`, **and for `auto`/unset** — an unknown language must stay CJK-capable.

Degradation semantics (`FontSet.degraded`) — a missing font is a **recorded** condition:

| Condition | Effect |
|---|---|
| CJK requested but no CJK face found | `degraded = True`, a note is attached, `spec.meta.degraded_fonts` is set |
| `degraded` at build time | `export_report.font.degraded = true`; `result.json.limitations` gains the font-fallback sentence |
| No Latin face at all | "falling back to the built-in width table"; measurement still works |
| Face unreadable for glyph checking | only characters outside the built-in ASCII table can be reported missing |

**Glyph coverage is gate L, not a footnote.** `missing_glyphs(text, path)` returns the characters
the resolved face cannot draw (`fontTools` cmap → `matplotlib.ft2font` → built-in ASCII
heuristic); `glyph_coverage_ok(text, fs)` succeeds only if a resolved face covers every non-space
character. A CJK label whose glyph is missing is **`E917`**: record it in
`limitations`/`uncertainty`, ship `status: "partial"`, and never pass a page full of tofu boxes.
If no coverage backend is available, the gate says **unavailable** — not "pass".

Measurement (`measure.py`) has three tiers and never raises: (1) `fontTools` `hmtx`+`head.unitsPerEm`
+`cmap` (exact advance widths, kerning ignored); (2) `matplotlib.ft2font` only if matplotlib is
already imported or `GA_USE_FT2FONT=1` (importing matplotlib can cost tens of seconds and would
eat the 900 s budget); (3) a built-in Helvetica/Arial AFM width table (ASCII only). Widths are
computed at a 12 pt reference and scaled linearly, so they are independent of accumulation order —
a prerequisite for gate G.

The SVG carries a real fallback chain (`family_css`) built from the resolved family plus the Latin
and CJK fallbacks, so it stays openable on a machine without the exact face used to lay it out.
The **PDF** is the format that must not depend on the viewer: gate H requires real embedded font
programmes in the PDF bytes.

---

## 4. Colour

* Default categorical palette `okabe-ito`, in the CVD- and grey-aware order used by the renderer:
  `#0072B2 #E69F00 #009E73 #000000 #F0E442 #D55E00 #CC79A7 #56B4E9`.
  The order matters: the first four are separated in L\* as well as hue, so a greyscale print can
  still tell them apart. Beyond four categories the grey floor cannot be met from this set alone,
  and gate C says so with measured numbers rather than passing silently.
* Alternative categorical palette `grayscale-safe`: `#1a1a1a #4d4d4d #808080 #b3b3b3`.
* Sequential ramps for colorbars/heatmaps: `viridis`, `batlow`, `cividis`, `grey`.
* `palette.colors` overrides `palette.id` when present.

Accessibility is enforced, not hoped for:

* **Gate C** — categorical colours stay distinguishable under protan/deutan/tritan simulation
  (ΔE ≥ 6), grayscale ΔL\* ≥ 5, text contrast ≥ 4.5:1.
* **Gate D** — the legend item multiset equals the drawn series/category multiset, a colorbar's
  declared `vmin`/`vmax` equal the data min/max, and units are present.

Colour choice is therefore a *checkable* part of the figure: if a figure needs more than four
categorical colours, the honest moves are to switch to `grayscale-safe`, use shapes/hatches, or
split the panel — not to squeeze a fifth hue past the gate.

---

## 5. Determinism

* **Gate G** re-exports the **already-built scene** and compares sha256 for each of SVG/PNG/PDF.
  It covers the export step only.
* The stronger claim — the **whole chain** (`ingest → layout → export`) reproduces byte-identical
  artefacts — is what `chart.py selftest` checks by running the chain twice.
* The same rule extends to `mark_data_map.json` and `source_map.json`: deterministic ids and
  sorted rows only; no timestamps, no RNG, no dict-order dependence, no PDF `/ID` from the clock.

If a run differs between two builds, treat it as a bug: find the nondeterministic input
(system time, RNG, dict order, parallel iteration) rather than retrying until the hashes match.

---

## 6. Export failure

An export that cannot be produced is **exit `2`** with `build-error.json` next to the output and a
reason in `export_report.json`; it is never silently reported as a delivered artefact. If only an
optional format fails (PPTX/EPS/TIFF), the required SVG/PNG/PDF still ship and the failure is
recorded as `unavailable` with its reason. If PNG or PDF itself cannot be produced, the run is not
`ok`: ship the SVG (still an editable master source), state the missing export in `limitations`,
and set `status: "partial"`.

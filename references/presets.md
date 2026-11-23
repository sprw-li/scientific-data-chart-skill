# Journal presets

A preset fixes the *physical* style of a figure — page width, type sizes, line weights, palette,
margins, panel-label case, tick style and font stack — so the same `spec.json` can be rendered for
Nature, Cell, Science, Elsevier, IEEE or a neutral generic page without rewriting the science.

* `chart.py presets --list` prints the shipped ids and their parameters (exit `0`).
* `chart.py build --preset <id>` (and `chart.py run --preset <id>`) applies the preset and writes
  the resolved values into `figure_spec.resolved.json`.
* An **unknown id is `E916`** (exit `4`) and never silently falls back to the default.
* A preset never overrides a value you set explicitly in `meta.canvas`; it supplies the defaults
  around it and enforces an absolute floor (type never goes below 7 pt body / 8 pt panel title /
  11 pt main title, per `scene.MIN_*_PT`).

Sources of truth: `work/CONTRACT.md` §8 and `presets/*.json`. The table below was read from the
shipped preset files; each file also carries its own `source` and per-parameter `param_source`.

---

## 1. The shipped presets

| id | page single (mm) | page double (mm) | default height (mm) | dpi | default columns | panel label | body pt |
|---|---|---|---|---|---|---|---|
| `nature-single` | 89.0 | 183.0 | 80 | 300 | 1 | lower `a` | 7 |
| `nature-double` | 89.0 | 183.0 | 100 | 300 | 2 | lower `a` | 7 |
| `science` | 55.0 | 120.0 | 70 | 300 | 1 | upper `A` | 7 |
| `cell` | 85.0 | 174.0 | 80 | 300 | 1 | upper `A` | 7 |
| `elsevier` | 90.0 | 190.0 | 90 | 300 | 1 | upper `A` | 7 |
| `ieee` | 88.9 | 181.9 | 80 | 300 | 1 | lower `a` | 8 |
| `generic` | 180.0 | 180.0 | 120 | 300 | 1 | lower `a` | 8 |

Every preset also defines `line_widths_pt{axis,data,error,frame}`,
`margins_mm{left,right,top,bottom}`, `palette_id`, `font_family_stack` **and**
`cjk_fallback_stack`, `tick_style{direction,length_mm,label_size_pt}`, `notes` and `source`.

`--preset` and `meta.canvas.h_mm` are independent: the preset supplies the width and the height
default, and an explicit `meta.canvas` wins. To see exactly what was applied, read
`figure_spec.resolved.json` after `build`, and `export_report.json` for the measured page
(`page_mm`) and pixel size.

---

## 2. Field contract per preset

```jsonc
{
  "id": "nature-single",
  "name": "Nature (single column)",
  "default_columns": 1,
  "canvas_mm": {"single": 89.0, "double": 183.0, "height": 80.0},
  "dpi": 300,
  "font_sizes_pt": {"body": 7.0, "label": 7.0, "panel_title": 8.0, "main_title": 11.0, "legend": 6.0},
  "line_widths_pt": {"axis": 0.5, "data": 1.0, "error": 0.75, "frame": 0.5},
  "palette_id": "okabe-ito",
  "margins_mm": {"left": 11.0, "right": 3.0, "top": 3.0, "bottom": 8.0},
  "panel_label_style": {"case": "lower", "weight": "bold", "position": "top-left", "size_pt": 8.0},
  "font_family_stack": ["Arial", "Helvetica", "DejaVu Sans", "sans-serif"],
  "cjk_fallback_stack": ["Microsoft YaHei", "SimHei", "Noto Sans SC", "Source Han Sans SC", "sans-serif"],
  "tick_style": {"direction": "out", "length_mm": 1.0, "label_size_pt": 7.0},
  "notes": "...",
  "source": "...",
  "param_source": {"<field>": {"kind": "guideline|convention", "detail": "..."}}
}
```

`param_source` is not decoration: every presets file states, per parameter, whether the value
comes from a **journal author guideline** (with the URL in `source`) or is a working
**`convention`**. Where a guideline gives only a minimum ("no smaller than 5 pt"), the shipped
value is a working size inside that range and is labelled `convention`.

---

## 3. Which preset to pick

* The case names a journal → that preset (`--preset cell`).
* The case names only a page width in mm → `generic`, plus `meta.canvas.w_mm`/`h_mm`.
* A single-column figure for a journal that is not listed → `nature-single` (89 mm) or
  `ieee` (88.9 mm) if the target is IEEE-style.
* A wide multi-panel data figure → `nature-double` (183 mm, 2 columns) or `elsevier` (190 mm).
* No journal information at all → `generic` (180 × 120 mm) and say so in `limitations` rather
  than inventing a house style.

Never claim a figure "meets the journal's requirements" because a preset was applied — the preset
is a starting point and its provenance is stated per parameter. Gate H proves the *export*
parameters (dpi, page, vector, fonts), not editorial compliance.

---

## 4. Fonts and CJK

Each preset carries two stacks:

* `font_family_stack` — Latin text, resolved in order; the resolved family is recorded in
  `export_report.json` (`font.family_css`).
* `cjk_fallback_stack` — used for CJK labels, resolved in order too.

Gate **L** asks whether the resolved font actually contains each CJK glyph used on the page. If
not, the run reports **`E917`**, records the fallback in `limitations` and is `partial` — the
figure is never passed with tofu boxes. If no glyph-coverage backend is available at all, the
check reports **unavailable** rather than "pass"; see
[`exports-fonts-colour.md`](exports-fonts-colour.md) §3.

---

## 5. Adding or changing a preset

1. Add `presets/<id>.json` with every field above **and** a `param_source` entry per parameter.
2. `chart.py presets --list` must list it; an unknown id must still be `E916`.
3. Re-run `chart.py selftest`; if the layout's absolute type floors change, update
   `scene.MIN_*_PT` and this page together.
4. Update this table with the values you actually wrote (do not copy the numbers from a journal
   guideline that uses a different page).

Determinism note: preset values are plain data; the same preset + same spec must produce
byte-identical artefacts (`figure.svg`/`.png`/`.pdf` and both maps).

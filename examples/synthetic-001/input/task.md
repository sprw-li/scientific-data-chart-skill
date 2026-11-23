# SYNTHETIC EXAMPLE — NOT AN OFFICIAL CASE

Build the data figure for this work order from the three supplied materials.

* grouped bar chart: cell viability per condition, bars = group mean, error bars = 1 SD, sample
  size n shown, y axis labelled with its unit;
* line chart: mitochondrial ROS time course, time axis in hours, one line per condition;
* mark the comparison the source note reports — the note does not state a p-value, so do not
  invent one;
* deliver an editable layered SVG source, real PNG and PDF exports, and the two-way
  mark<->source map (`mark_data_map.json`), a `source_map.json` and `result.json`.

Return every field named in the case's `output_contract`.

The material is a synthetic hypoxia/cardiomyocyte study (invented for this example). It is
internally consistent, but it is not a real measurement and must not be cited as one. The draft
caption in `case.json.figure.stale_caption` is out of date — use `materials/methods.md` and the
tables as the source of truth.

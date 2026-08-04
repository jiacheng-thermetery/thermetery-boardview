# tools/

Standalone dev scripts used while building and debugging the parsers,
matcher, and renderer. These are **not** a shipped part of the app (see
`packaging/boardviewer.spec`, which excludes this directory) and carry
no API-stability guarantee — invoke them directly, don't import from
them.

Run from the repo root as a module, e.g. `python -m tools.spotcheck`.

- `convert_rules.py` — translate a power-sequencing diagnostic `.xlsx`
  (sheet = platform) into `rules.yaml`.
  `python -m tools.convert_rules <input.xlsx> [<input2.xlsx> ...] -o <output.yaml>`
- `inspect_xlsx.py` — dump the raw structure (sheets, merges, cell
  colors, comments) of an `.xlsx`, used while designing the rules
  converter. `python -m tools.inspect_xlsx <file.xlsx> [--sheet NAME | --all]`
- `schematic_text_probe.py` — run `src/schematic_text.py`'s
  `extract_index()` over a directory (or single file) of schematic PDFs
  and print a per-file summary; catches regressions in the title/signal
  extractor. `python -m tools.schematic_text_probe [dir|file.pdf] [--titles] [--signals N]`
- `signal_match_probe.py` — run `src/signal_match.py`'s fuzzy matcher
  against a rules YAML and a set of schematic PDFs, reporting per-tier
  hit rates vs. a naive exact-match baseline.
  `python -m tools.signal_match_probe [PDF ...] [--rules R.yaml] [--sample N]`
- `spotcheck.py` — print one platform's entry from `private/rules.yaml`
  by name prefix. `python -m tools.spotcheck <prefix>`
- `tvw_phase3_test.py` — exercise the lazy topology hook on `BoardModel`
  (build cost, caching, broken-net detection, geometry/point lookups)
  across the three reference TVW boards plus one GENCAD board.
  `python -m tools.tvw_phase3_test`
- `tvw_mfp_verify.py` — verify `tvw_master_fp` footprint pin-position
  reconstruction against each board's real pad records, reporting
  match rate at several tolerances; exits non-zero below 90%.
  `python -m tools.tvw_mfp_verify`
- `walker_render_test.py` — render-tier + frame-time benchmark: loads
  each reference board through `walker.make_board_canvas` and times
  redraws at a few zoom levels with traces enabled.
  `python -m tools.walker_render_test [--gl-probe-only]`

# Third-party licenses

This directory holds the verbatim license texts and attribution headers for
third-party code that has been adapted into this project. Each file documents
which file(s) in the project are derived from which upstream, and reproduces
the upstream license in full as required.

| File | Upstream | Used in |
| ---- | -------- | ------- |
| `OpenBoardView-MIT.txt` | https://github.com/OpenBoardView/OpenBoardView (MIT) | `xzzpcb_parser.py` — parser logic and record schema for XZZPCB `.pcb` files; `rc6_native.c` — C port of `FZFile::decode` (RC6-CFB-1 cipher used by ASUS `.fz` files); `fz_parser.py` (the RC6 cipher pieces only — `_RC6_PARITY`, `_validate_fz_key`, `_rc6_decode` — pure-Python fallback for the same cipher) |
| `dhuertas-DES-MIT.txt`  | https://github.com/dhuertas/DES (MIT)               | `xzzpcb_parser.py` (pure-Python DES fallback) and `xzz_native.c` (the C fast path compiled into `xzz_native.dll`) — both port the same DES reference implementation |

The project itself is **LGPL-3.0-or-later** (`LICENSE` at the repository root).

The fully ported source files (`xzzpcb_parser.py`, `xzz_native.c`, and
`rc6_native.c`) carry an SPDX `MIT` tag — they're full ports of MIT-
licensed upstream code, so we keep them under MIT for upstream consistency.
Anyone can reuse those three files under MIT terms outside this project.

`fz_parser.py` is a hybrid: the RC6 cipher pieces are a port of
OpenBoardView/FZFile.cpp (MIT) but the surrounding Extracta record parser,
BoardModel mapping, cache, and keyfile resolver are original work. The file
as a whole carries SPDX `LGPL-3.0-or-later` with the OpenBoardView copyright
reproduced in its header to satisfy MIT's "include this notice in
substantial portions" clause for the ported portions.

The rest of the codebase is LGPL.

The MIT permission notices reproduced here satisfy MIT's "copyright notice
and this permission notice shall be included" clause.

## Courtesy attribution (no derived code)

`inflex/teboviewformat` (https://github.com/inflex/teboviewformat, MIT,
Paul Daniels) is reproduced in `THIRD_PARTY_NOTICES.md` but is intentionally
**not** in the table above and does **not** have a `LICENSES/<file>.txt` of
its own. Reason: this project ships no code derived from teboviewformat —
the TVW parser (`tvw_parser.py`, `tvw_master_fp.py`, `tvw_seg_27_unified_v3.py`,
`tvw_topology.py`, `tvw_native.c`, and `TVW_FORMAT.md`) is independent
reverse engineering that *started* from inflex's pioneering work but does
not include or derive from its source. The full
notice is reproduced in `THIRD_PARTY_NOTICES.md` as a courtesy and to make
the lineage of the format research clear.

When adding new third-party code:
1. Drop the upstream LICENSE verbatim into this directory, named
   `<Upstream>-<License>.txt`.
2. Add a row to the table above.
3. Add a short header comment to the top of any derived source file pointing
   here (see existing parsers for the established style).

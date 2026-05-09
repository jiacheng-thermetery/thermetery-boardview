# Third-party licenses

This directory holds the verbatim license texts and attribution headers for
third-party code that has been adapted into this project. Each file documents
which file(s) in the project are derived from which upstream, and reproduces
the upstream license in full as required.

| File | Upstream | Used in |
| ---- | -------- | ------- |
| `OpenBoardView-MIT.txt` | https://github.com/OpenBoardView/OpenBoardView (MIT) | `xzzpcb_parser.py` — parser logic and record schema for XZZPCB `.pcb` files |
| `dhuertas-DES-MIT.txt`  | https://github.com/dhuertas/DES (MIT)               | `xzzpcb_parser.py` (pure-Python DES fallback) and `xzz_native.c` (the C fast path compiled into `xzz_native.dll`) — both port the same DES reference implementation |

The project itself is **LGPL-3.0-or-later** (`LICENSE` at the repository root).
The two ported source files (`xzzpcb_parser.py` and `xzz_native.c`) carry an
SPDX `MIT` tag — they're full ports of MIT-licensed upstream code, so we keep
them under MIT for upstream consistency. Anyone can reuse those two files
under MIT terms outside this project; the rest of the codebase is LGPL.

The MIT permission notices reproduced here satisfy MIT's "copyright notice
and this permission notice shall be included" clause.

When adding new third-party code:
1. Drop the upstream LICENSE verbatim into this directory, named
   `<Upstream>-<License>.txt`.
2. Add a row to the table above.
3. Add a short header comment to the top of any derived source file pointing
   here (see existing parsers for the established style).

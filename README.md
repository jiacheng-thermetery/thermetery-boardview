# Boardviewer

A pan/zoom viewer for PCB boardview files, with component and net browsing. This initial release should have full support for Gigabyte `.tvw` files. 

Loads:

| Format                     | Extension(s)              | Notes                          |
| -------------------------- | ------------------------- | ------------------------------ |
| GENCAD 1.4                 | `.cad`                    | Mentor / Teradyne ASCII        |
| OpenBoardView ASCII        | `.brd`, `.brd2`, `.bv`    | BRD2 (modern) and legacy BRD   |
| Teboview (Gigabyte)        | `.tvw`                    | binary; pin↔net + traces       |

For the TVW binary format, see [TVW_FORMAT.md](TVW_FORMAT.md) for a working spec
(file macro layout, coordinate system, master-footprint pin-position decoder,
and the 38-byte pad records that carry the pin↔net mapping).

## Running

```
python viewer.py                         # opens a file picker
python viewer.py path/to/board.tvw       # loads directly
```

## Controls

- **Mouse drag** — pan
- **Mouse wheel** — zoom around the cursor
- **Home** — reset view (fit-to-window)
- **L** — flip layer (TOP ↔ BOTTOM, mirrored horizontally)
- **T** — toggle trace rendering
- **Click an IC** — select; pins render as yellow dots
- **Click a pin** — focus pin; Net tab fills with everything else on that net
- **Click a row in Net tab** — jump to that pin (auto-flips layer)
- **Component / Net search** in toolbar — autocomplete by refdes or net name
- **View menu** — mirror X, rotate 90° CW/CCW

## Renderer tiers

The trace layer on a dense modern board can have 40k+ segments. tk.Canvas
can't draw that fast enough per frame. The viewer picks the fastest
renderer the environment supports:

1. **GPU tier** — `pyopengltk` + `PyOpenGL` + `skia-python` + `numpy`. Sub-10ms
   frames at heavy zoom on 13k-trace boards. Default when available.
2. **CPU tier** — `skia-python` + `numpy`. Off-screen Skia surface composited
   into a binary PPM and handed to `tk.PhotoImage` (Tcl's C image loader).
   ~30-50 ms per frame.
3. **Fallback** — plain `tk.Canvas` per-segment lines. Works without any
   pip dependencies, but expect single-digit FPS on busy boards.

`pip install -r requirements.txt` gets you the GPU tier on most machines.

## Layout

```
boardview.py            unified loader — extension dispatch + content sniff
gencad_parser.py        .cad parser  → BoardModel
brd_parser.py           .brd / .brd2 / .bv parser  → BoardModel
tvw_parser.py           .tvw parser  → BoardModel
tvw_master_fp.py        TVW master-footprint pin-position decoder
tvw_topology.py         TVW trace topology graph (segments + polylines)
tvw_seg_27_unified_v3.py  TVW polyline / chain block scanner
viewer.py               Tk app + board canvas (CPU + GL tiers)
TVW_FORMAT.md           Working spec for the Teboview binary format
```

## Status

Working. Loads tested against:
- MSI MS-7680 Rev 5.1 (GENCAD)
- Gigabyte Z490 VISION G, X570 GAMING X, B550 AORUS PRO AC (TVW)

## License

LGPL-3.0-or-later. See [LICENSE](LICENSE) for the full text.

You can use this code as a library in proprietary tools and you can
redistribute the viewer as part of larger works. If you modify the
LGPL'd files themselves, those modifications must be released under
LGPL-3.0-or-later.

Copyright (C) 2026 Thermetery Technology LLC.

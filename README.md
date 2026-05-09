# `.tvw` Boardviewer

A pan/zoom viewer for PCB boardview files, with component and net browsing.
Multi-layer trace inspection on GPU PCBs (TOP, BOTTOM, INNER_1..N), pin↔net
mapping for every supported format, cross-layer trace highlight when a net
is selected.

Loads:

| Format                     | Extension(s)              | Notes                          |
| -------------------------- | ------------------------- | ------------------------------ |
| GENCAD 1.4                 | `.cad`                    | Mentor / Teradyne ASCII        |
| OpenBoardView ASCII        | `.brd`, `.brd2`, `.bv`    | BRD2 (modern) and legacy BRD   |
| Teboview (Gigabyte)        | `.tvw`                    | binary; pin↔net + traces       |
| Allegro Extracta `.fz`     | `.fz`                     | binary; ASRock = zlib-only, ASUS = RC6+zlib (needs an FZKey at `private/fz_key.txt`) |
| XZZPCB (MSI / repair shops)| `.pcb`                    | binary, DES-encrypted; needs an XZZ key (see THIRD_PARTY_NOTICES.md) |

For the TVW binary format, see [TVW_FORMAT.md](TVW_FORMAT.md) for a working spec
(file macro layout, coordinate system, master-footprint pin-position decoder,
and the 38-byte pad records that carry the pin↔net mapping).

## Running

```
python viewer.py                         # opens a file picker
python viewer.py path/to/board.tvw       # loads directly
```

## Acknowledgements
I would like to thank the collaborative team effort at OpenBoardView at https://github.com/OpenBoardView/OpenBoardView/issues/291, especially the user inflex, for the tremendous pioneering work that he has done in the reverse engineering process. 

## Controls

- **Mouse drag** — pan
- **Mouse wheel** — zoom around the cursor
- **Home** — reset view (fit-to-window)
- **L** — cycle through available layers. On 2-layer boards (most TVW
  motherboards, all GENCAD/BRD/XZZ files) this is just TOP↔BOTTOM. On
  multi-layer boards (GPU PCBs once trace topology is built) it walks
  through TOP, BOTTOM, INNER_1, INNER_2, ... and wraps. The toolbar
  **Layer** dropdown does the same thing with direct selection.
- **T** — toggle trace rendering. First press on a multi-layer board
  builds the topology (3-6 s) and populates the inner-layer entries
  in the Layer dropdown. Selecting a net then highlights it across
  every layer the trace touches (current layer in bright yellow,
  off-current layers in their layer's palette colour) so you can see
  the full cross-layer path of the net.
- **Click an IC** — select; pins render as yellow dots
- **Click a pin** — focus pin; Net tab fills with everything else on that net
- **Click a row in Net tab** — jump to that pin (auto-flips layer)
- **Component / Net search** in toolbar — autocomplete by refdes or net name
- **View menu** — mirror X, rotate 90° CW/CCW

When viewing an inner copper layer, components (which only exist on TOP/
BOTTOM in the data model) render as faint outline ghosts so you can still
see what the trace runs under without losing the layer you're inspecting.

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
tvw_native.{c,dll,py}   optional C-extension fast path for the scanners
                          (~600× speedup on cold loads; .py shim falls
                          back to pure Python if the .dll is absent)
fz_parser.py            .fz parser (Allegro Extracta)  → BoardModel
                          Independent Python implementation; format
                          spec verified against OpenBoardView's
                          FZFile.cpp. ASRock files parse without a
                          key; ASUS files need an FZKey.
rc6_native.{c,dll}      optional C fast-path port of FZFile::decode
                          (RC6-CFB-1 cipher used by ASUS .fz). MIT-
                          licensed; attribution in THIRD_PARTY_NOTICES.md
                          and LICENSES/. Pure-Python fallback in
                          fz_parser.py runs when the .dll is absent
                          (~150× slower but works).
xzzpcb_parser.py        .pcb parser (XZZPCB V1.0)  → BoardModel
                          Python port of OpenBoardView's XZZPCBFile.cpp
                          and dhuertas/DES; both MIT, attribution in
                          THIRD_PARTY_NOTICES.md and LICENSES/.
xzz_native.{c,dll,py}   optional C-extension fast path for the DES
                          decryption used by xzzpcb_parser (~100× speedup
                          on cold loads — full board in ~0.3 s vs 30-60 s
                          in pure Python; .py shim falls back if the .dll
                          is absent). Decrypted plaintext is never
                          written to disk: leaving proprietary file
                          contents in a cache is an IP/leakage hazard.
viewer.py               Tk app + board canvas (CPU + GL tiers)
TVW_FORMAT.md           Working spec for the Teboview binary format
```

## Status

Working. Loads tested against:
- MSI MS-7680 Rev 5.1, MSI MS-17E7, ASUS ROG Maximus Z690 EXTREME,
  Dell Alienware Area 51M / 17 R4 (GENCAD)
- Apple iMac A1311 820-2492-A (BRD)
- Gigabyte Z490 VISION G, X570 GAMING X, B550 AORUS PRO AC (TVW)
- Gigabyte GV-N780OC-3GD GPU (TVW, 10-layer — exercises the multi-
  layer trace cycle and cross-layer highlight)
- ASRock X370P-RO4, Z390 Pro4, Z97X Killer (FZ, zlib-only path)
- ASUS PRIME Z370-A, ASUS GTX 1080 Ti STRIX (FZ, also zlib-only —
  not all ASUS files are RC6-encrypted)
- MSI V389/7913/7914/7A05/7A06 series, PS5 EDM-010 (XZZPCB)

## Known Issues

None currently tracked.

## License

LGPL-3.0-or-later. See [LICENSE](LICENSE) for the full text.

You can use this code as a library in proprietary tools and you can
redistribute the viewer as part of larger works. If you modify the
LGPL'd files themselves, those modifications must be released under
LGPL-3.0-or-later.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution
of the upstream projects whose file-format documentation informed the
parsers in this repository.

Copyright (C) 2026 Thermetery Technology LLC.

# Thermetery Boardviewer

A pan/zoom viewer for PCB boardview files, with component and net
browsing. Multi-layer trace inspection on GPU PCBs (TOP, BOTTOM,
INNER_1..N), pin↔net mapping for every supported format, and cross-layer
trace highlight when a net is selected.

The viewer (`viewer.py`) is the focus of this project. The repository
also contains an **experimental, work-in-progress repair walker**
(`walker.py`) layered on the same parser/renderer core — see
[Experimental: repair walker](#experimental-repair-walker-work-in-progress)
at the bottom. It is early-stage and not the maintained surface of this
repo.

## Supported formats

| Format                      | Extension(s)              | Notes                          |
| --------------------------- | ------------------------- | ------------------------------ |
| GENCAD 1.4                  | `.cad`                    | Mentor / Teradyne ASCII        |
| OpenBoardView ASCII         | `.brd`, `.brd2`, `.bv`    | BRD2 (modern) and legacy BRD   |
| Teboview — Gigabyte         | `.tvw`                    | binary; pin↔net + traces       |
| Teboview — Compal / Lenovo  | `.tvw`                    | binary; auto-detected variant (Thinkpad NM-B501, etc.); connector + chip-class decoding |
| Allegro Extracta `.fz`      | `.fz`                     | binary; ASRock = zlib-only, ASUS = RC6+zlib (needs an FZKey at `private/fz_key.txt`) |
| XZZPCB (MSI / repair shops) | `.pcb`                    | binary, DES-encrypted; needs an XZZ key (see THIRD_PARTY_NOTICES.md) |

The loader (`boardview.py`) dispatches by extension and content sniff.
For the TVW binary format see [TVW_FORMAT.md](TVW_FORMAT.md) /
[TVW_FORMAT.html](TVW_FORMAT.html) — a working spec covering the file
macro layout, coordinate system, master-footprint pin-position decoder,
the 38-byte pad records that carry the pin↔net mapping, and (from the
Compal/Lenovo work) the chip-class enum and connector pin transform.

### Encrypted formats — supplying a key

ASUS `.fz` (RC6) and XZZPCB `.pcb` (DES) files are encrypted and need a key,
which is not shipped. The default location is `private/fz_key.txt` (ASUS,
44 hex words) / `private/XZZ_Key.txt` (XZZ, 16 hex digits). If that file is
missing you can still supply the key:

- **In the viewer** — opening such a board pops a dialog to paste the key,
  then offers to save it to `private/` for next time (opt-in; `private/` is
  gitignored).
- **Environment** — set `FZ_KEY` or `XZZPCB_KEY`.
- **CLI** — `python viewer.py board.fz --key "<key>"`.

Without a key, XZZ boards still load their cleartext outline + test pads;
an ASUS `.fz` cannot open at all (its whole body is encrypted).

## Install

The viewer runs on stdlib + tkinter alone; the optional packages in
`requirements.txt` enable the faster trace renderers.

```
pip install -r requirements.txt
```

| File | Adds | For |
| --- | --- | --- |
| `requirements.txt` | numpy, skia-python, pyopengltk, PyOpenGL, tkinterdnd2 | renderer tiers + drag-drop |
| `requirements-walker.txt` | PyYAML, openpyxl, pymupdf, anthropic, openai, keyring | **experimental** walker only — skip unless you want to try it |

## Running

```
python viewer.py                         # opens a file picker
python viewer.py path/to/board.tvw       # loads directly
```

`python viewer.py --smoke-test` does a headless import/launch check.

## Controls

- **Drag** to pan, **wheel** to zoom around the cursor, **Home** to
  fit-to-window.
- **L** cycles layers — TOP↔BOTTOM on 2-layer boards (most TVW
  motherboards, all GENCAD/BRD/XZZ files), or TOP/BOTTOM/INNER_1/
  INNER_2/… on multi-layer boards once trace topology is built. The
  toolbar **Layer** dropdown selects directly.
- **T** toggles trace rendering. The first press on a multi-layer board
  builds topology (3–6 s) and populates the inner-layer entries.
  Selecting a net then highlights it across every layer it touches
  (current layer bright yellow, off-layers in their layer's palette
  colour) so you can see the full cross-layer path.
- **Click an IC** to select (pins render as yellow dots); **click a
  pin** to focus it and fill the Net tab with everything else on that
  net; **click a Net-tab row** to jump (auto-flips layer).
- **Component / Net search** in the toolbar autocompletes by refdes or
  net name; **View menu** does mirror-X and 90° rotate.
- **Drag-drop** a boardview file onto the canvas to open it (requires
  the optional `tkinterdnd2`; without it the menu / Ctrl+O picker still
  works).

When viewing an inner copper layer, TOP/BOTTOM components render as
faint ghost outlines so you keep spatial context without losing the
layer you're inspecting.

**Native-DLL preflight** — on launch the viewer probes the three native
fast-path DLLs and prints a one-time stderr warning (with per-format
slowdown and the build command) for any that are missing, so a fresh
checkout that forgot to compile them is obvious before you open a board.

## Renderer tiers

A dense modern board's trace layer can top 40k segments; `tk.Canvas`
can't draw that fast per frame. The viewer picks the fastest available
tier at startup:

1. **GPU** — `pyopengltk` + `PyOpenGL` + `skia-python` + `numpy`.
   Sub-10 ms frames at heavy zoom on 13k-trace boards. Default when
   available.
2. **CPU** — `skia-python` + `numpy`. Off-screen Skia surface composited
   into a PPM and handed to `tk.PhotoImage` (Tcl's C image loader).
   ~30–50 ms/frame.
3. **Fallback** — plain `tk.Canvas` lines. No pip deps; single-digit FPS
   on busy boards.

`pip install -r requirements.txt` gets you the GPU tier on most machines.

## Native fast paths

Three optional C extensions accelerate cold-load. Each has a pure-Python
fallback in its `.py` wrapper, so the viewer works without compiling
anything (you just wait longer):

| DLL              | Speeds up                    | Penalty when absent              |
| ---------------- | ---------------------------- | -------------------------------- |
| `tvw_native.dll` | TVW pad/poly/net scanners    | +1–2 s per `.tvw`                |
| `xzz_native.dll` | XZZPCB DES decryption        | +30–60 s per `.pcb` (~100× hit)  |
| `rc6_native.dll` | ASUS `.fz` RC6 decryption    | +6 s per ASUS `.fz`              |

Build scripts (`build_*.bat`) compile each `.c` against MinGW-w64 GCC;
the exact invocation is in each `.c` file's header. Decrypted plaintext
(XZZPCB / ASUS FZ) is never written to disk — caching proprietary file
contents is an IP/leakage hazard.

## Layout

```
# Core (shared by the viewer and the experimental walker)
boardview.py              unified loader — extension dispatch + content sniff
gencad_parser.py          .cad  → BoardModel
brd_parser.py             .brd / .brd2 / .bv  → BoardModel
fz_parser.py              .fz (Allegro Extracta)  → BoardModel  (RC6 pieces MIT)
xzzpcb_parser.py          .pcb (XZZPCB V1.0)  → BoardModel  (port of XZZPCBFile.cpp + dhuertas/DES, MIT)
tvw_parser.py             .tvw  → BoardModel; variant dispatch (Gigabyte vs Compal/Lenovo)
tvw_compal.py             Compal/Lenovo TVW decoder (chip enumeration, pin↔net, connectors)
tvw_master_fp.py          TVW master-footprint pin-position decoder
tvw_topology.py           TVW trace topology graph (segments + polylines)
tvw_seg_27_unified_v3.py  TVW polyline / chain block scanner
ratsnest.py               synthetic ratsnest topology (when no trace geometry)
rc6_native.{c,dll}        optional C fast-path for RC6 (ASUS .fz)
xzz_native.{c,dll,py}     optional C fast-path for DES (XZZPCB)
tvw_native.{c,dll,py}     optional C fast-path for TVW scanners

# Viewer
viewer.py                 Tk app + board canvas (CPU + GL tiers), drag-drop, DLL preflight

# Experimental walker (work in progress — see bottom)
walker.py                 diagnostic walk app (canvas + chat + walk-step engine)
linker.py                 rules YAML × BoardModel → linked probe instructions
signal_match.py           fuzzy signal-name matcher
schematic_text.py         schematic PDF → per-page signal index
convert_rules.py          .xlsx → rules.yaml converter
*_probe.py / *_test.py    CLI probes and test drivers

# Docs / licensing
TVW_FORMAT.md / .html     Teboview binary format spec
THIRD_PARTY_NOTICES.md    MIT attributions (OpenBoardView, dhuertas/DES, inflex/teboviewformat)
LICENSES/                 verbatim license texts for embedded MIT code
```

## Status

Boardview parsing verified across:

- MSI MS-7680 Rev 5.1, MSI MS-17E7, ASUS ROG Maximus Z690 EXTREME,
  Dell Alienware Area 51M / 17 R4 (GENCAD)
- Apple iMac A1311 820-2492-A (BRD)
- Gigabyte Z490 VISION G, X570 GAMING X, B550 AORUS PRO AC (TVW, Gigabyte)
- Gigabyte GV-N780OC-3GD GPU (TVW, 10-layer — exercises the multi-layer
  trace cycle and cross-layer highlight)
- Lenovo Thinkpad T480 NM-B501 (TVW, Compal/Lenovo variant — connector +
  chip-class decoding, JKBL1/JTAG2 pin↔net verified)
- ASRock X370P-RO4, Z390 Pro4, Z97X Killer (FZ, zlib-only)
- ASUS PRIME Z370-A, ASUS GTX 1080 Ti STRIX (FZ, zlib-only)
- MSI V389 / 7913 / 7914 / 7A05 / 7A06 series, PS5 EDM-010 (XZZPCB)

## License

LGPL-3.0-or-later. See [LICENSE](LICENSE).

You can use this code as a library in proprietary tools and redistribute
the viewer as part of larger works. If you modify the LGPL'd files
themselves, those modifications must be released under LGPL-3.0-or-later.
The embedded RC6 / DES fast paths and the XZZPCB parser carry their
upstream MIT notices; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
and [LICENSES/](LICENSES/).

Copyright (C) 2026 Thermetery Technology LLC.

## Acknowledgments

Thanks to the collaborative reverse-engineering effort at
[OpenBoardView issue #291](https://github.com/OpenBoardView/OpenBoardView/issues/291),
especially **inflex**, whose pioneering work and
[teboviewformat](https://github.com/inflex/teboviewformat) repo were the
starting point for the TVW research behind [TVW_FORMAT.md](TVW_FORMAT.md).

This project also embeds MIT-licensed **code** (not just format docs):

- **Chloridite** and the **OpenBoardView contributors** —
  `xzzpcb_parser.py` is a Python port of `XZZPCBFile.cpp`; the RC6-CFB-1
  cipher in `rc6_native.c` and parts of `fz_parser.py` port `FZFile.cpp`.
- **Dani Huertas** ([dhuertas/DES](https://github.com/dhuertas/DES)) —
  the DES reference used to decrypt XZZPCB part-records (pure-Python
  fallback in `xzzpcb_parser.py` and the C fast path in `xzz_native.c`).

---

## Experimental: repair walker (work in progress)

> ⚠️ **Early-stage and not the focus of this repo.** The walker is folded
> in here to keep it on the shared parser/renderer core, but it is
> incomplete, lightly tested, and subject to change. Use the viewer for
> anything you rely on. The former standalone `thermetery-repair-walker`
> repository is being retired in favour of this one.

`walker.py` layers a guided power-sequencing **repair walk** on top of
the viewer's canvas: load a rules YAML and a schematic PDF, pick a
chipset platform, and it steps you through which net to probe, where on
the board, the voltage / resistance to expect, and what to investigate
next based on the reading. An optional Claude / OpenAI / Ollama chat
panel bundles the current step, selected component, and recent results
into context.

```
pip install -r requirements-walker.txt
python walker.py                                   # blank window; drag a board in
python walker.py path/to/board.cad                 # opens a board; pick rules from the menu
python walker.py rules.yaml board.cad <platform>   # full triple
```

The rules YAML is user-supplied (not bundled), structured roughly:

```yaml
platforms:
  Intel 6X-7X:                      # the platform prefix you pass on the CLI
    sections:
      no_trigger:                   # failure mode this section covers
        stages:
          G3:                       # power stage
            signals:
              - net: VCCRTC
                voltage: "2.8V-3.3V"
                resistance: "~400Ω"
                kind: critical_rail
```

`convert_rules.py` can translate an `.xlsx` (sheet = platform) into this
shape. `signal_match.py` fuzzy-matches canonical rule names (`VCCRTC`,
`RSMRST#`) against decorated schematic/boardview net names;
`schematic_text.py` indexes a vector-PDF schematic into a `signal →
pages` lookup. The walker autosaves per-platform progress to
`private/walker_state_*.json` and resumes on relaunch.

Expect rough edges. Issues or breakage in the walker are not a
regression in the viewer.

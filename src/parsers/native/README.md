# Native Acceleration Core (TVW / RC6 / XZZ)

High-performance C implementations of the dominant cold-load and
topology-construction paths of the boardview parsers. Optional: every
caller falls back to the pure-Python implementation transparently when
a library is missing (`src/app_common.py`'s `check_native_dlls` warns
about the slowdown at app startup).

## Design goals

- **Bit-for-bit fidelity** with the corresponding pure-Python scanners
  and builders. Every validation branch, early-exit heuristic, and
  coordinate range check is reproduced in the same order, so the two
  paths are interchangeable. Fidelity is maintained by construction and
  exercised against the local board corpus with the dev tools in
  `tools/` (e.g. `tvw_mfp_verify.py`, `tvw_phase3_test.py`) — there is
  no runtime self-check, so treat any change to either side as a
  change to both.
- **Zero Python state** while the native functions execute (ctypes
  releases the GIL for the call).
- **Bounded memory and time.** Output is written into caller-supplied
  arrays; temporary internal tables are freed before return.
- **Portability.** C99 with only `stdint.h`, `stddef.h`, `string.h`,
  `stdlib.h`, `stdio.h`. Builds under MSYS2 UCRT64, Android NDK, and
  standard Linux toolchains.

## Modules

| File | Responsibility |
|------|----------------|
| `tvw_native.c` | Pad-run discovery (38/54-byte strides), net-table location, polyline and tagged-polyline scanners, segment runs, chip-header + pin-record probe sweeps, and the complete TraceGraph topology builder (spatial hash, union-find, via bridging, same-net pad fusion, pad-to-trace fusion, net propagation). |
| `rc6_native.c` | RC6-CFB decrypt hot path for ASUS `.fz`. |
| `xzz_native.c` | DES decrypt hot path for XZZPCB `.pcb`. |

## Build

Usually nothing: `src/native_build.py` compiles whatever is missing on launch
(see the repo README's *Building* section). The two manual routes below stay
available, and a library either of them produces is left alone by the
autobuild — it only ever replaces one it built itself.

```bash
meson setup build && meson compile -C build
```

or by hand (Windows, MSYS2 UCRT64) — the same flags the autobuild uses:

```bash
gcc -O3 -shared -static-libgcc -Wl,--strip-all -o tvw_native.dll tvw_native.c
```

`-O3` plus modern GCC/Clang auto-vectorises the `memchr`-driven byte
searches; no hand-written intrinsics. The Android build cross-compiles
the same sources via the NDK (`build_android_native.bat`) into
`android/app/src/main/jniLibs/`.

## Python integration contract

The wrappers (`tvw_native.py`, `xzz_native.py`, and the RC6 loader in
`fz_parser.py`) return the same object shapes as the pure-Python
originals, or `None`/unavailable when the library is absent — callers
decide the fallback. Output record layouts are mirrored exactly by the
corresponding `ctypes.Structure` definitions; offsets are `uint64_t`.

Quality notes:

- Public entry points document the exact Python function they replace.
- Underflow/overflow guards on every candidate start calculation.
- Floor-division matches Python's `//` semantics for negative
  coordinates (critical for spatial hashing).
- The topology builder preserves the exact order of operations of the
  Python `_build`, enabling deterministic comparison of broken-net
  counts and `net_at_point` results.

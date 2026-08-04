# Native Acceleration Core (TVW / RC6 / XZZ)

This directory contains the high-performance C implementations that accelerate the dominant cold-load and topology-construction paths of the Thermetery boardview parsers.

## Design Goals

- **Bit-for-bit fidelity** with the corresponding pure-Python scanners and builders. Every validation branch, early-exit heuristic, and coordinate range check is reproduced in the same order.
- **Zero Python state** while the native functions execute. Callers may drop the GIL (ctypes automatically does so for `CFUNCTYPE` declarations).
- **Bounded memory and time**. All output is written into caller-supplied arrays; the functions never allocate beyond temporary internal tables that are freed before return.
- **Portability**. Pure C99 with only `<stdint.h>`, `<stddef.h>`, `<string.h>`, `<stdlib.h>`, and `<stdio.h>`. Builds cleanly under MSYS2 UCRT64, Android NDK, and standard Linux toolchains.

## Modules

| File | Responsibility |
|------|----------------|
| `tvw_native.c` | Pad-run discovery (38/54-byte strides), net-table location, polyline block and tagged-polyline scanners, segment runs, chip-header + pin-record probe sweeps, and the complete TraceGraph topology builder (spatial hash, union-find, via bridging, same-net pad fusion, pad-to-trace fusion, net propagation). |
| `rc6_native.c` | RC6-specific hot path (if present). |
| `xzz_native.c` | XZZ PCB format acceleration. |

## Build

```bash
# Windows (MSYS2 UCRT64)
gcc -O3 -shared -static-libgcc -Wl,--strip-all -o tvw_native.dll tvw_native.c

# Android (via NDK, already integrated in the Gradle build)
# See android/app/src/main/jniLibs/
```

`-O3` plus modern GCC/Clang auto-vectorises the `memchr`-driven byte searches into AVX2 (or NEON) loops; no hand-written intrinsics are required.

## Python Integration Contract

The Python wrappers (`tvw_native.py`, etc.) perform a one-shot equality check against the pure-Python reference on first load. Only after that check passes is the native path used. This guarantees that any future change to either side that breaks fidelity is detected immediately.

Output record layouts are mirrored exactly by the corresponding `ctypes.Structure` definitions. All offsets are `uint64_t` so >4 GiB buffers remain safe (even though boardview files are far smaller).

## Quality Notes (target 9.5)

- All public entry points are documented with the exact Python function they replace.
- Underflow / overflow guards are present on every candidate start calculation.
- Floor-division matches Python’s `//` semantics for negative coordinates (critical for spatial hashing).
- Temporary hash tables and vote maps are sized as the next power of two and freed before return; no leaked allocations on the success or error paths.
- The topology builder preserves the exact order of operations of the original Python `_build`, enabling deterministic comparison of broken-net counts and `net_at_point` results.

Future work that would push quality still higher includes formal property-based tests against a corpus of real boards and continuous fuzzing of the scanners.

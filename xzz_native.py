# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""ctypes glue for xzz_native.dll — DES decryption fast path used by
xzzpcb_parser to unwrap XZZPCB part/pin records.

The DLL implements one function: `xzz_des_decrypt_buffer`. Pure-Python
DES is ~30-60 s on a typical motherboard's encrypted records; the C
build drops that to a few hundred ms. We deliberately do NOT cache
decrypted bytes to disk (proprietary plaintext sitting in a cache file
is an IP/leakage hazard). Each parse re-decrypts in memory.

The DLL is loaded lazily and validated via `xzz_des_selftest` (the
Rivest test vector). If anything fails — DLL missing, load error,
self-test mismatch — `decrypt()` returns None and the caller falls
back to the pure-Python implementation.
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Optional


_LIB: Optional[ctypes.CDLL] = None
_LOAD_ATTEMPTED = False


def _candidate_names() -> list[str]:
    if sys.platform.startswith("win"):
        return ["xzz_native.dll"]
    if sys.platform == "darwin":
        return ["xzz_native.dylib", "libxzz_native.dylib"]
    return ["xzz_native.so", "libxzz_native.so"]


def _load() -> Optional[ctypes.CDLL]:
    """Lazy DLL loader. Tries once; subsequent calls return the cached
    handle (or None if the first attempt failed)."""
    global _LIB, _LOAD_ATTEMPTED
    if _LOAD_ATTEMPTED:
        return _LIB
    _LOAD_ATTEMPTED = True

    here = Path(__file__).resolve().parent
    for name in _candidate_names():
        path = here / name
        if not path.exists():
            continue
        try:
            lib = ctypes.CDLL(str(path))
        except OSError:
            continue

        # Bind signatures.
        lib.xzz_des_decrypt_buffer.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_uint64,
            ctypes.c_char_p,
        ]
        lib.xzz_des_decrypt_buffer.restype = ctypes.c_int32

        lib.xzz_des_selftest.argtypes = []
        lib.xzz_des_selftest.restype = ctypes.c_int32

        # Refuse a miscompiled build.
        if lib.xzz_des_selftest() != 0:
            continue

        _LIB = lib
        return lib
    return None


def available() -> bool:
    return _load() is not None


def decrypt(buf: bytes, key: int) -> Optional[bytes]:
    """Decrypt `buf` (multiple of 8 bytes; trailing 0..7 are copied
    through) using the 64-bit DES `key`. Returns the decrypted bytes,
    or None if the DLL isn't available — caller should fall back to
    the pure-Python implementation in that case."""
    lib = _load()
    if lib is None:
        return None
    n = len(buf)
    out = ctypes.create_string_buffer(n)
    rc = lib.xzz_des_decrypt_buffer(buf, n, ctypes.c_uint64(key), out)
    if rc != 0:
        return None
    return out.raw[:n]


__all__ = ["available", "decrypt"]

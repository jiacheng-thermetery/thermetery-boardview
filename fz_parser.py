# SPDX-License-Identifier: LGPL-3.0-or-later
#
# Mixed-origin file. The outer parser (Allegro Extracta record decoder,
# BoardModel mapping, decrypted-text cache, keyfile resolver, native-
# DLL bridge) is original work and falls under the project's
# LGPL-3.0-or-later licence. The RC6 cipher pieces — `_RC6_PARITY`,
# `_validate_fz_key`, and `_rc6_decode` — are a faithful Python port
# of OpenBoardView's `src/openboardview/FileFormats/FZFile.cpp` and
# remain under their original MIT terms. The C fast-path equivalent
# ships separately as `rc6_native.c` (also SPDX MIT). Full MIT
# permission notice: LICENSES/OpenBoardView-MIT.txt. See also
# THIRD_PARTY_NOTICES.md.
#
#   Copyright (c) 2016 Chloridite and OpenBoardView contributors
#                     (RC6 parity table, parity check, RC6-CFB-1 decode)
#   Copyright (C) 2026 Thermetery Technology LLC
#                     (everything else: API, Extracta parser, BoardModel
#                      mapping, cache, keyfile resolver, native bridge)

"""Parse a `.fz` boardview file (ASRock / ASUS) into a BoardModel.

Format overview (verified against OpenBoardView's FZFile.cpp):

```
+0..3              uint32 LE  uncompressed CONTENT size
+4..              body — either:
                   (a) zlib stream (ASRock files; magic 78 9C / 78 DA at +4)
                   (b) RC6-encrypted body (ASUS files; otherwise)
[after content]    8-byte mini-header + zlib DESCRIPTION stream
end-4..end         uint32 LE  description-block size (mini-header + zlib)
```

After RC6 decoding (or directly for ASRock), inflating the content yields
the **Allegro Extracta** plaintext: a sequence of schema-prefixed,
pipe-delimited records.

```
A!FIELD1!FIELD2!...     declares the schema for the records that follow
S!val1!val2!...         data row matching the most recent A! schema
```

Every Gigabyte/ASRock/ASUS file we have follows the same 8-section layout:
unit decl, components, pin/net (with explicit pin coords!), vias, per-
component graphics (silk, place bounds), board geometry, LOGOInfo,
UnDrawSym. We only care about the components and pin/net sections —
that's enough to populate `BoardModel.components`, `model.shapes`, and
`model.signals`.

`.fz` files don't carry trace routing — the class-prefixed graphics
table only contains BOARD GEOMETRY/OUTLINE. So we don't attach a
topology loader; `topology_available` reads False and the viewer
hides the trace overlay UI cleanly.

ASUS files require an FZKey (44 × 32-bit words) which is NOT shipped
with OpenBoardView and is NOT in this repo. The user must supply one
in `private/fz_key.txt` (one hex word per line, comments with #). If
the file is RC6-encrypted and no key is configured, parsing raises
with a helpful message.
"""
from __future__ import annotations

import ctypes
import hashlib
import math
import os
import pickle
import struct
import sys
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from gencad_parser import BoardModel, Component, Shape


# Validation parity for the 44-word RC6 key. From OpenBoardView/FZFile.cpp.
# Each candidate key word's bit-parity (folded XOR + complement) must
# match the corresponding entry. Lets us fail fast on a bad key.
_RC6_PARITY: List[int] = [
    0, 1, 1, 0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 1, 1, 0,
    1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 0, 0,
    0, 1, 0, 0, 0, 1, 0, 0, 1, 1, 0, 1,
]


# --------------------------------------------------------------------------
# Native RC6 (C extension via ctypes)
# --------------------------------------------------------------------------
#
# `rc6_native.dll` is a tiny C port of FZFile::decode that is ~75× faster
# than the inlined pure-Python implementation. We load it lazily and fall
# back to the Python version if the DLL is missing or fails to load.
#
# Build (one-time, Windows MSYS2 UCRT64):
#     gcc -O3 -shared -static-libgcc -o rc6_native.dll rc6_native.c
# A `build_rc6.bat` next to the source automates the PATH setup.

_NATIVE_RC6 = None  # callable(uint8*, size_t, uint32*) | None


def _load_native_rc6():
    """Locate and bind `rc6_native.{dll,so,dylib}` if available.

    Returns a ctypes-wrapped function or None. Cached after first call so
    the cost is paid exactly once."""
    global _NATIVE_RC6
    if _NATIVE_RC6 is not None:
        return _NATIVE_RC6 if _NATIVE_RC6 is not False else None

    here = Path(__file__).resolve().parent
    if sys.platform.startswith("win"):
        names = ["rc6_native.dll"]
    elif sys.platform == "darwin":
        names = ["rc6_native.dylib", "librc6_native.dylib"]
    else:
        names = ["rc6_native.so", "librc6_native.so"]
    for n in names:
        p = here / n
        if not p.exists():
            continue
        try:
            lib = ctypes.CDLL(str(p))
            fn = lib.rc6_decode
            fn.argtypes = [
                ctypes.c_char_p,           # source pointer (mutable buffer)
                ctypes.c_size_t,           # size
                ctypes.POINTER(ctypes.c_uint32),  # 44-word key
            ]
            fn.restype = None
            _NATIVE_RC6 = fn
            return fn
        except (OSError, AttributeError):
            continue
    _NATIVE_RC6 = False
    return None


def _rc6_decode_native(source: bytearray, key: List[int]) -> bool:
    """Try the native RC6. Returns True on success, False if unavailable
    (caller should fall through to the Python implementation)."""
    fn = _load_native_rc6()
    if fn is None:
        return False
    KeyArr = ctypes.c_uint32 * 44
    karr = KeyArr(*[k & 0xFFFFFFFF for k in key])
    # `c_char_p` from a bytearray exposes a writable pointer that the C
    # function modifies in place — we then see those changes back in
    # `source` because bytearrays share the buffer.
    buf_ptr = (ctypes.c_char * len(source)).from_buffer(source)
    fn(buf_ptr, len(source), karr)
    return True


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

class FZKeyError(ValueError):
    """Raised when an ASUS (RC6-encrypted) FZ file needs a key that wasn't
    supplied, or that failed the parity check. ``reason`` is ``"missing"``
    or ``"invalid"`` so a UI can decide whether to prompt for a key or
    report a bad one."""

    def __init__(self, message: str, *, reason: str = "missing") -> None:
        super().__init__(message)
        self.reason = reason


def parse(path: Path, key=None) -> BoardModel:
    """Parse a `.fz` boardview file into a BoardModel."""
    p = Path(path)
    buf = bytearray(p.read_bytes())
    if len(buf) < 12:
        raise ValueError(f"{p.name}: file too short for FZ format")

    text = _decode_fz(buf, source_name=p.name, source_path=p, key=key)
    return _parse_extracta(text)


# --------------------------------------------------------------------------
# Decoding (zlib for ASRock; RC6 + zlib for ASUS)
# --------------------------------------------------------------------------

def _decode_fz(buf: bytearray, *, source_name: str,
               source_path: Optional[Path] = None, key=None) -> str:
    """Decode an FZ file's body into the Extracta text content.
    Description block is parsed but discarded (it's BoM-style metadata)."""
    # Skip the 4-byte uncompressed-size header. Detect whether the body
    # starts with zlib magic — if so it's already plaintext (ASRock).
    is_zlib = (len(buf) >= 6
               and buf[4] == 0x78
               and buf[5] in (0x9C, 0xDA))

    if not is_zlib:
        key = _resolve_fz_key(key)
        if key is None:
            raise FZKeyError(
                f"{source_name}: ASUS-style FZ file (RC6-encrypted body). "
                f"This needs an FZKey (44 x 32-bit hex words). Supply one via "
                f"private/fz_key.txt, the FZ_KEY environment variable, or the "
                f"key= argument (the viewer prompts for it on open).",
                reason="missing",
            )
        if not _validate_fz_key(key):
            raise FZKeyError(
                f"{source_name}: FZKey failed parity check — the 44 words "
                f"don't match OpenBoardView's expected parity array. "
                f"Verify the key.",
                reason="invalid",
            )

        # Skip the 6+ second pure-Python RC6 if we've decoded this
        # file before with this key. Cache lives next to the source
        # file as `.fz.cache` so it travels with the boardview.
        cached_text = _cache_load(source_path, buf, key)
        if cached_text is not None:
            return cached_text

        # RC6 decode runs over the WHOLE buffer (matches OpenBoardView's
        # `FZFile::decode(file_buf, buffer_size)`). The 4-byte size
        # header is also encrypted; skipping it desyncs the keystream.
        # Native C path is ~75× faster; falls through to pure Python
        # when rc6_native.dll isn't available.
        if not _rc6_decode_native(buf, key):
            _rc6_decode(buf, key)

    # The body is now plaintext zlib + (optional description block).
    # decompressobj lets us see the boundary: it consumes only the
    # content stream and exposes the rest as `unused_data`.
    d = zlib.decompressobj()
    content = d.decompress(bytes(buf[4:]))
    text = content.decode("latin-1", errors="replace")

    # Save the decrypted text alongside the file so subsequent loads
    # can skip RC6 entirely. (No-op for ASRock files — those are fast
    # already so we don't bother caching them.)
    if not is_zlib:
        _cache_save(source_path, key=key, text=text)

    return text


# --------------------------------------------------------------------------
# Decrypted-text cache
# --------------------------------------------------------------------------

_CACHE_MAGIC = b"FZC2"  # bump if format changes


def _cache_path_for(source_path: Optional[Path]) -> Optional[Path]:
    if source_path is None:
        return None
    try:
        return source_path.with_suffix(source_path.suffix + ".cache")
    except (TypeError, ValueError):
        return None


def _key_fingerprint(key: List[int]) -> bytes:
    """Stable 16-byte hash of the FZKey for cache validation."""
    h = hashlib.sha256()
    for w in key:
        h.update(struct.pack("<I", w & 0xFFFFFFFF))
    return h.digest()[:16]


def _cache_load(source_path: Optional[Path], buf: bytes,
                key: List[int]) -> Optional[str]:
    """Return cached plaintext if the cache file matches the source +
    key fingerprint exactly; else None.

    Validates source size, source mtime_ns, and the key fingerprint.
    Any mismatch ⇒ re-decrypt.
    """
    cache_path = _cache_path_for(source_path)
    if cache_path is None or not cache_path.exists():
        return None
    try:
        with cache_path.open("rb") as f:
            magic = f.read(4)
            if magic != _CACHE_MAGIC:
                return None
            (size, mtime_ns) = struct.unpack("<QQ", f.read(16))
            keyhash = f.read(16)
            text_len = struct.unpack("<Q", f.read(8))[0]
            text_bytes = f.read(text_len)
        st = os.stat(source_path)
        if st.st_size != size:
            return None
        if st.st_mtime_ns != mtime_ns:
            return None
        if keyhash != _key_fingerprint(key):
            return None
        return text_bytes.decode("latin-1", errors="replace")
    except (OSError, struct.error, UnicodeDecodeError):
        return None


def _cache_save(source_path: Optional[Path], *,
                key: List[int], text: str) -> None:
    cache_path = _cache_path_for(source_path)
    if cache_path is None:
        return
    try:
        st = os.stat(source_path)
        body = text.encode("latin-1", errors="replace")
        with cache_path.open("wb") as f:
            f.write(_CACHE_MAGIC)
            f.write(struct.pack("<QQ", st.st_size, st.st_mtime_ns))
            f.write(_key_fingerprint(key))
            f.write(struct.pack("<Q", len(body)))
            f.write(body)
    except OSError:
        # Read-only directory or similar; not critical — just skip cache.
        pass


def _parse_fz_key_text(text: str) -> Optional[List[int]]:
    """Tokenize a textual FZKey into exactly 44 32-bit words, or return None
    if the text doesn't yield 44 hex words. Forgiving: accepts 0x prefixes,
    whitespace / comma / semicolon separators, and '#' line comments. Shared
    by the key file, the FZ_KEY env var, and keys pasted into the viewer."""
    words: List[int] = []
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0]
        stripped = stripped.replace(",", " ").replace(";", " ")
        for tok in stripped.split():
            t = tok.strip()
            if t.lower().startswith("0x"):
                t = t[2:]
            if not t:
                continue
            try:
                words.append(int(t, 16) & 0xFFFFFFFF)
            except ValueError:
                continue
    return words if len(words) == 44 else None


def _resolve_fz_key(explicit=None) -> Optional[List[int]]:
    """Resolve a 44-word FZKey from, in order: an explicit key (a list of 44
    ints, or a string to tokenize), the FZ_KEY environment variable, then
    private/fz_key.txt. Returns None if none yielded a 44-word key. Mirrors
    xzzpcb_parser._resolve_key. Parity is NOT checked here -- the caller
    validates, so it can distinguish 'missing' from 'invalid'."""
    if explicit is not None:
        if isinstance(explicit, str):
            return _parse_fz_key_text(explicit)
        # Otherwise assume an iterable of ints (already-parsed 44 words).
        try:
            words = [int(w) & 0xFFFFFFFF for w in explicit]
        except (TypeError, ValueError):
            return None
        return words if len(words) == 44 else None
    env = os.environ.get("FZ_KEY")
    if env:
        words = _parse_fz_key_text(env)
        if words is not None:
            return words
    return _load_fz_key()


def _load_fz_key() -> Optional[List[int]]:
    """Look for a 44-word FZKey configured locally. Returns None if not found.

    Forgiving format — accepts any whitespace layout that produces 44
    32-bit hex tokens. All of these work:
      ``0xa1b2c3d4 0xdeadbeef ...``  (single line, 0x-prefixed)
      ``a1b2c3d4\\ndeadbeef\\n...``    (one token per line, no prefix)
      ``a1b2c3d4, deadbeef, ...``     (commas / mixed punctuation)
    Line-level `#` introduces a comment to end-of-line.
    """
    candidates = [
        Path("private") / "fz_key.txt",
        Path(__file__).parent / "private" / "fz_key.txt",
    ]
    for p in candidates:
        if not p.exists():
            continue
        words = _parse_fz_key_text(p.read_text())
        if words is not None:
            return words
    return None


def _validate_fz_key(key: List[int]) -> bool:
    """Verify a candidate FZKey matches OpenBoardView's parity expectation."""
    for i, word in enumerate(key):
        tmp = word & 0xFFFFFFFF
        tmp ^= tmp >> 16
        tmp ^= tmp >> 8
        tmp ^= tmp >> 4
        tmp ^= tmp >> 2
        tmp ^= tmp >> 1
        tmp = (~tmp) & 1
        if tmp != _RC6_PARITY[i]:
            return False
    return True


def _rotl32(a: int, b: int) -> int:
    a &= 0xFFFFFFFF
    b &= 31
    if b == 0:
        return a
    return ((a << b) | (a >> (32 - b))) & 0xFFFFFFFF


def _rc6_decode(source: bytearray, key: List[int]) -> None:
    """Port of OpenBoardView's `FZFile::decode` (RC6-CFB-1 mode).

    In place: each byte of `source` is XOR'd with the low byte of A
    after a fresh 20-round RC6 forward block on (A, B, C, D). State is
    reloaded from a 16-byte sliding window of the original ciphertext
    bytes (CFB-1 with the saved ciphertext as feedback).

    Optimisations vs the textbook port:
      * The 16-byte ibuf is held as a single 128-bit Python int and
        shifted in one op (`(ibuf >> 8) | (current << 120)`) — avoids
        a 15-byte slice copy per iteration.
      * `rotl32(x, 5)` is inlined as `((x<<5)|(x>>27)) & M` — saves a
        function call (the inner loop runs 20 × n times, so this is
        the hottest expression in the file).
      * All 44 key words are bound to local variables outside the
        loop — Python's LOAD_FAST is materially faster than indexing
        into a list every round.

    Single-thread by necessity: CFB-1 is sequential — byte N+1's
    keystream depends on byte N's ciphertext via the ibuf shift. No
    GIL/process trick parallelises this.
    """
    M = 0xFFFFFFFF
    A = B = C = D = 0
    ibuf = 0  # 128-bit; byte k of the C version lives in bits k*8..k*8+7

    # Local-bind every key word — LOAD_FAST in the hot loop.
    (k0, k1, k2, k3, k4, k5, k6, k7, k8, k9, k10, k11, k12, k13, k14, k15,
     k16, k17, k18, k19, k20, k21, k22, k23, k24, k25, k26, k27, k28, k29,
     k30, k31, k32, k33, k34, k35, k36, k37, k38, k39, k40, k41, k42,
     k43) = key

    n = len(source)
    for pos in range(n):
        B = (B + k0) & M
        D = (D + k1) & M
        # 20 rounds — manually unrolled in pairs of (round, key index).
        # Inlining would balloon the source ~20×; the local-bound key
        # tuple plus the inlined rotl32(_, 5) below already win us most
        # of the speedup.
        for k_even, k_odd in (
            (k2, k3), (k4, k5), (k6, k7), (k8, k9),
            (k10, k11), (k12, k13), (k14, k15), (k16, k17),
            (k18, k19), (k20, k21), (k22, k23), (k24, k25),
            (k26, k27), (k28, k29), (k30, k31), (k32, k33),
            (k34, k35), (k36, k37), (k38, k39), (k40, k41),
        ):
            tt = (B * (2 * B + 1)) & M
            t = ((tt << 5) | (tt >> 27)) & M
            uu = (D * (2 * D + 1)) & M
            u = ((uu << 5) | (uu >> 27)) & M
            ur = u & 31
            tr = t & 31
            xa = A ^ t
            xc = C ^ u
            A = (((xa << ur) | (xa >> (32 - ur))) & M) + k_even & M if ur \
                else (xa + k_even) & M
            C = (((xc << tr) | (xc >> (32 - tr))) & M) + k_odd & M if tr \
                else (xc + k_odd) & M
            A, B, C, D = B, C, D, A
        A = (A + k42) & M
        C = (C + k43) & M

        current = source[pos]
        source[pos] = current ^ (A & 0xFF)

        # Slide ibuf left by one byte (drop the low byte, append the
        # ciphertext byte at position 15).
        ibuf = (ibuf >> 8) | (current << 120)
        A = ibuf & M
        B = (ibuf >> 32) & M
        C = (ibuf >> 64) & M
        D = (ibuf >> 96) & M


# --------------------------------------------------------------------------
# Extracta text → BoardModel
# --------------------------------------------------------------------------

def _parse_float(s: str) -> float:
    """Parse a float field that may use comma as decimal separator.

    ASUS exports use European-style commas in some — but not all — number
    fields (e.g. `4446,01` = 4446.01 in pin coordinates, while many
    integers stay as `3653`). We normalise the comma to a dot and let
    Python parse it. Empty string treats as 0.0.
    """
    s = (s or "").strip()
    if not s:
        return 0.0
    return float(s.replace(",", "."))


def _parse_extracta(text: str) -> BoardModel:
    """Parse the schema-prefixed pipe-delimited records and build a BoardModel.

    Components and pin records are extracted; per-component graphics,
    vias, board geometry, LOGOInfo, and UnDrawSym are skipped (not
    needed for the viewer today). The result has no topology loader —
    `.fz` files don't carry trace routing.
    """
    model = BoardModel()
    current_columns: Optional[List[str]] = None
    pin_records: List[Dict[str, str]] = []

    def _split(line: str) -> List[str]:
        # Drop leading 'A!' / 'S!' and the trailing empty cell from the
        # final '!'. Preserves embedded backslash-escape sequences in
        # field values verbatim (we don't try to interpret them).
        parts = line.split("!")
        if parts and parts[-1] == "":
            parts = parts[:-1]
        return parts[1:]

    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("A!"):
            current_columns = _split(line)
            continue
        if not line.startswith("S!") or current_columns is None:
            continue
        values = _split(line)
        if len(values) != len(current_columns):
            # Mismatched row — schema-vs-row count off. Skip silently
            # rather than poison the model. Could log if needed.
            continue
        rec = dict(zip(current_columns, values))

        # Components section: schema starts with REFDES.
        #   ASRock: REFDES, COMP_INSERTION_CODE, SYM_NAME, SYM_MIRROR,
        #           SYM_ROTATE, SYM_X, SYM_Y         (7 cols, has positions)
        #   ASUS:   REFDES, COMP_INSERTION_CODE, SYM_NAME, SYM_MIRROR,
        #           SYM_ROTATE                      (5 cols, NO positions)
        # For ASUS we leave x/y as NaN now and fill them in from the
        # centroid of the component's pin coords once pins are parsed.
        if (current_columns[:1] == ["REFDES"]
                and "SYM_NAME" in current_columns
                and "SYM_ROTATE" in current_columns):
            refdes = rec.get("REFDES", "")
            if not refdes:
                continue
            try:
                rotation = _parse_float(rec.get("SYM_ROTATE", "0"))
            except ValueError:
                rotation = 0.0
            if "SYM_X" in current_columns:
                try:
                    x = _parse_float(rec.get("SYM_X", "0"))
                    y = _parse_float(rec.get("SYM_Y", "0"))
                except ValueError:
                    x, y = 0.0, 0.0
            else:
                # ASUS variant — sentinel to be filled later.
                x = math.nan
                y = math.nan
            mirror = rec.get("SYM_MIRROR", "NO").strip().upper()
            layer = "BOTTOM" if mirror == "YES" else "TOP"
            sym_name = rec.get("SYM_NAME", "")
            model.components[refdes] = Component(
                refdes=refdes,
                x=x, y=y, rotation=rotation,
                layer=layer,
                # `shape` doubles as the shape-table key; we'll set it
                # to the per-instance shape we synthesise below.
                # `device` keeps the human-readable footprint name for
                # the Component info panel.
                shape=refdes,
                device=sym_name,
            )
            continue

        # Pin/net section: schema starts with NET_NAME + has PIN_X.
        if (current_columns[:1] == ["NET_NAME"]
                and "PIN_X" in current_columns):
            pin_records.append(rec)
            continue

        # Other sections (vias, graphics, board geometry, TESTVIA, etc.)
        # are parsed-and-skipped intentionally.

    # Build signals + populate the per-instance shapes.
    pins_by_refdes: Dict[str, List[Dict[str, str]]] = {}
    for rec in pin_records:
        refdes = rec.get("REFDES", "").strip()
        pin_num = rec.get("PIN_NUMBER", "").strip()
        if not refdes or not pin_num:
            continue
        pins_by_refdes.setdefault(refdes, []).append(rec)

    # Decide per-component canonical pin names. ASRock-style Allegro
    # Extracta exports carry distinct PIN_NUMBER values per pin and we
    # honour them. ASUS-style exports routinely emit the same single
    # placeholder ("0") for every pin of a multi-pin component — see
    # GitHub issue #2. The Net tab's tk.Treeview iid is `{refdes}__{pin}`
    # so duplicate names within one component would silently fail to
    # insert (`tk.TclError` is swallowed), and `BoardCanvas.select_pin`
    # linear-scans by pin name and always lands on the first match —
    # together those make every pin row past the first unclickable.
    # When we detect within-component collisions, rebuild that one
    # component's pin names as a 1..N sequence in record order; the
    # underlying file didn't carry a real number so any consistent
    # injection is acceptable, and integer indices keep the Pin column
    # in the Net tab readable. ASRock components (no collisions) keep
    # their original PIN_NUMBER values.
    canonical_pin_name: Dict[Tuple[str, int], str] = {}
    for refdes, recs in pins_by_refdes.items():
        raw_names = [r["PIN_NUMBER"].strip() for r in recs]
        if len(set(raw_names)) == len(raw_names):
            for i, name in enumerate(raw_names):
                canonical_pin_name[(refdes, i)] = name
        else:
            for i in range(len(recs)):
                canonical_pin_name[(refdes, i)] = str(i + 1)

    # Build signals from the canonical names (not the raw PIN_NUMBER).
    # Walk records in pins_by_refdes insertion order so the per-record
    # index matches what the Shape build below assigns to local_pins.
    for refdes, recs in pins_by_refdes.items():
        for i, rec in enumerate(recs):
            net = rec.get("NET_NAME", "").strip()
            if net:
                model.signals.setdefault(net, []).append(
                    (refdes, canonical_pin_name[(refdes, i)])
                )

    # ASUS variant: components carry no SYM_X/SYM_Y. Fill them in from
    # the centroid of the component's pin world coords. Centroid is the
    # most robust single anchor — single-pin components fall back to
    # that pin's position, multi-pin chips end up at the geometric
    # centre of the footprint (close enough that pin offsets — which we
    # derive next via un-rotation — come out symmetric).
    drop_refs: List[str] = []
    for refdes, comp in model.components.items():
        if not (math.isnan(comp.x) or math.isnan(comp.y)):
            continue
        comp_pins = pins_by_refdes.get(refdes, [])
        coords: List[Tuple[float, float]] = []
        for pr in comp_pins:
            try:
                px = _parse_float(pr.get("PIN_X", "0"))
                py = _parse_float(pr.get("PIN_Y", "0"))
            except ValueError:
                continue
            coords.append((px, py))
        if coords:
            comp.x = sum(c[0] for c in coords) / len(coords)
            comp.y = sum(c[1] for c in coords) / len(coords)
        else:
            # No pins, no position → can't render. Drop the component
            # so the bbox computation doesn't pick up NaN coords.
            drop_refs.append(refdes)
    for r in drop_refs:
        model.components.pop(r, None)

    # Synthesise components for refdes that appear ONLY in pin records
    # (not in the COMPONENTS section). ASUS files routinely omit major
    # connectors from the components table and only declare them
    # implicitly via pins — e.g. `LGA2066` (1,974 pins on the X299
    # CPU socket) and the eight `DIMM_*` slots (281 pins each).
    # Without this, the viewer silently drops the most prominent
    # parts of the board.
    for refdes, pins in pins_by_refdes.items():
        if refdes in model.components:
            continue
        coords = []
        for pr in pins:
            try:
                px = _parse_float(pr.get("PIN_X", "0"))
                py = _parse_float(pr.get("PIN_Y", "0"))
            except ValueError:
                continue
            coords.append((px, py))
        if not coords:
            continue
        cx = sum(c[0] for c in coords) / len(coords)
        cy = sum(c[1] for c in coords) / len(coords)
        # Rotation 0 + centroid anchor ⇒ pin offsets equal `pin - centroid`,
        # which round-trips exactly back to the original pin world coords
        # via the standard renderer formula. Layer defaults to TOP, since
        # connectors omitted from the components table are almost always
        # the front-side sockets (CPU, DIMM, PCIe, M.2).
        model.components[refdes] = Component(
            refdes=refdes, x=cx, y=cy, rotation=0.0,
            layer="TOP", shape=refdes, device=refdes,
        )

    # Synthesise one Shape per component instance. Pin offsets are
    # derived by un-rotating each pin's world coord against the
    # component's placement+rotation. Per-instance shapes (rather than
    # per-SYM_NAME) sidestep the mirror question: a SYM_NAME may appear
    # on both TOP and BOTTOM, and a single shared shape can't represent
    # both correctly. With per-instance shapes the round trip
    #   pin_world = comp.{x,y} + rot(dx, dy, comp.rotation)
    # is exact regardless of mirror state.
    for refdes, comp in model.components.items():
        comp_pins = pins_by_refdes.get(refdes, [])
        # Empty Shape entry even when no pins, so viewer code that
        # does `model.shapes.get(comp.shape)` has something to find.
        local_pins: List[Tuple[str, float, float]] = []
        if comp_pins:
            theta = math.radians(-comp.rotation)
            ct, st = math.cos(theta), math.sin(theta)
            for i, pr in enumerate(comp_pins):
                try:
                    px = _parse_float(pr.get("PIN_X", "0"))
                    py = _parse_float(pr.get("PIN_Y", "0"))
                except ValueError:
                    continue
                rx = px - comp.x
                ry = py - comp.y
                dx = rx * ct - ry * st
                dy = rx * st + ry * ct
                # Canonical name was decided above; use it here so the
                # Shape's pin list and `model.signals` share the same
                # names. Index `i` indexes the same `pins_by_refdes`
                # list both passes walked.
                local_pins.append(
                    (canonical_pin_name[(refdes, i)], dx, dy),
                )
        model.shapes[comp.shape] = Shape(name=comp.shape, pins=local_pins)

    return model


__all__ = ["parse"]

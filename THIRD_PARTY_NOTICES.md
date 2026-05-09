# Third-Party Notices

Most of this repository's parsers (`brd_parser.py`, `tvw_parser.py`,
`tvw_master_fp.py`, `tvw_seg_27_unified_v3.py`, `tvw_topology.py`) are
**independent implementations**, informed by file-format documentation
and reverse-engineering work in the upstream projects listed below.

Four pieces are direct ports of upstream MIT code rather than
independent rewrites:

  * `xzzpcb_parser.py` — Python port of OpenBoardView's
    `XZZPCBFile.cpp` (XZZ parser) **and** of dhuertas/DES (the DES
    routine used inside that parser to decrypt part / pin records).
  * `xzz_native.c` — C port of dhuertas/DES (the same DES routine,
    in C). Built into `xzz_native.dll` as the optional fast path for
    `xzzpcb_parser.py`; the pure-Python fallback inside the parser
    runs when the .dll is absent.
  * `rc6_native.c` — C port of OpenBoardView's `FZFile::decode`
    (RC6-CFB-1 cipher used by ASUS `.fz` files). Built into
    `rc6_native.dll` as the optional fast path for `fz_parser.py`.
  * `fz_parser.py` is a **hybrid** file. The Allegro Extracta record
    parser, BoardModel mapping, decrypted-text cache, and key-file
    resolver are independent. The RC6-CFB-1 cipher pieces inside it
    (`_RC6_PARITY`, `_validate_fz_key`, `_rc6_decode`) are a
    pure-Python port of `FZFile::decode` — same upstream as
    `rc6_native.c`, used as the slow fallback when the .dll is absent.
    The file as a whole is licensed LGPL-3.0-or-later because the
    surrounding code is original; the OpenBoardView MIT copyright is
    reproduced inside the file header alongside the LGPL-or-later
    SPDX line, and applies to the named functions.

All upstreams are MIT-licensed and explicitly permit this; the verbatim
license texts are reproduced both here and under [`LICENSES/`](LICENSES/).

The MIT notices and permission text below are reproduced for license
compliance and as a courtesy to downstream users. They apply to the
ported portions named above. The rest of this repository's own code
is licensed under LGPL-3.0-or-later (see [LICENSE](LICENSE)).

---

## OpenBoardView

- Upstream:  https://github.com/OpenBoardView/OpenBoardView
- Used for:
  - reference to the BRD / BRD2 ASCII boardview format
    (`brd_parser.py` — independent Python implementation, does not
    include or derive from OpenBoardView's C++ source).
  - reference to the Allegro Extracta `.fz` record schema
    (`fz_parser.py`'s record parser, BoardModel mapping,
    decrypted-text cache, and keyfile resolver — independent
    Python implementation, does not include or derive from
    `FZFile.cpp` for these parts).
  - **Python port** of `src/openboardview/FileFormats/FZFile.cpp`'s
    RC6-CFB-1 cipher used by ASUS `.fz` files, including the
    44-word RC6 round-key parity table and parity validation
    (`fz_parser.py`'s `_RC6_PARITY`, `_validate_fz_key`, and
    `_rc6_decode`). This is the pure-Python fallback path. The
    upstream MIT copyright notice is reproduced in the header of
    `fz_parser.py`; the file as a whole is LGPL because the
    surrounding code is original.
  - **C port** of the same `FZFile::decode` for the
    `rc6_native.dll` fast path used by `fz_parser.py` on ASUS
    files (`rc6_native.c`, ~hundreds of MB/s vs ~40 KB/s for the
    Python fallback).
  - **Python port** of `src/openboardview/FileFormats/XZZPCBFile.cpp`
    and `XZZPCBFile.h` for the XZZ `.pcb` parser
    (`xzzpcb_parser.py`). Format reversal credit per the upstream
    file header: @huertas (DES), @inflex, @MuertoGB, @slimeinacloak,
    @piernov, Thomas Lamy.
  - **Note on keys**: OpenBoardView does not ship working ASUS-FZ
    or XZZ-DES keys, and neither does this repository. Users supply
    their own (e.g. extracted from a proprietary viewer they already
    own) via `private/fz_key.txt` for ASUS .fz files, and
    `private/XZZ_Key.txt` or the `XZZPCB_KEY` environment variable
    for XZZ .pcb files. Without keys: ASRock-style .fz files still
    parse fully; XZZ files parse outline + test pads + net list,
    skipping encrypted part/pin records.
- License:   MIT

```
Copyright (c) 2016 Chloridite and OpenBoardView contributors

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
```

---

## dhuertas/DES

- Upstream:  https://github.com/dhuertas/DES
- Used for:  port of the DES algorithm reference implementation used
            by `xzzpcb_parser.py` to decrypt XZZ `.pcb` part / pin
            records. The same C implementation is reproduced inside
            OpenBoardView under `src/openboardview/Crypto/des.c`.
            Two ports, same attribution chain:
              - `xzzpcb_parser.des()` — pure-Python fallback;
              - `xzz_native.c` (built into `xzz_native.dll`) — the
                fast path, drops a full-board decrypt from ~30-60 s
                to ~0.3 s. Both ports preserve the dhuertas/DES
                copyright notice in their source headers as required
                by the MIT terms.
- License:   MIT

```
MIT License

Copyright (c) 2020 Dani Huertas

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
```

---

## inflex/teboviewformat

- Upstream:  https://github.com/inflex/teboviewformat
- Used for:  starting point for Teboview (`.tvw`) format research. The
            final TVW decode used by this project — chip-instance
            pre-32 metadata (chip XY in `i32[1],i32[0]`, rotation,
            instance index), the 38-byte pad records in the Custom_35
            and Custom_17 trace blocks (net id at offset +22), and the
            master-footprint coordinate transform that places each
            chip's pin geometry in world coordinates — was done
            independently and is not derived from teboviewformat's
            source. See [TVW_FORMAT.md](TVW_FORMAT.md) for the working
            spec.
- License:   MIT

```
Copyright (c) 2021 Paul Daniels

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
```

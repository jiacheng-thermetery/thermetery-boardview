# Third-Party Notices

This repository does **not** include source code from any third party.
The parsers (`brd_parser.py`, `tvw_parser.py`, `tvw_master_fp.py`,
`tvw_seg_27_unified_v3.py`, `tvw_topology.py`) are independent
implementations. They were informed by file-format documentation and
reverse-engineering work in the upstream projects listed below, which
this project gratefully acknowledges.

The MIT copyright notices and permission text below are reproduced as a
courtesy and to leave no ambiguity for downstream users. They do not
apply to any portion of this repository's own code, which is licensed
under LGPL-3.0-or-later (see [LICENSE](LICENSE)).

---

## OpenBoardView

- Upstream:  https://github.com/OpenBoardView/OpenBoardView
- Used for:  reference to the BRD / BRD2 ASCII boardview format
            (`brd_parser.py`). Our parser is an independent Python
            implementation; it does not include or derive from
            OpenBoardView's C++ source.
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

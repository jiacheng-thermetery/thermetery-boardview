# TVW (Gigabyte Teboview) binary format

Status: substantially understood as of 2026-05-07. Cracked by inspection
of three Gigabyte motherboard files (Z490 VISION G, X570 GAMING X, B550
AORUS PRO AC) cross-checked against their public PDF schematics.

Starting point: [inflex/teboviewformat](https://github.com/inflex/teboviewformat)
(MIT, Paul Daniels). The reverse engineering published there established the
direction; the final decode below — chip-instance pre-32 metadata, the 38-byte
pad records inside Custom_35/17, the master-footprint coordinate transform —
was developed independently from cross-board inspection and ground-truth
comparison against BoardViewer.exe. See `THIRD_PARTY_NOTICES.md` for the
courtesy reproduction of the upstream MIT notice.

This is a working spec, not a complete one. Sections marked **(unverified)**
or **(partial)** describe patterns observed but not exhaustively confirmed.

---

## 1. Coordinate system

- All coordinates are signed 32-bit integers in **file units**.
- 1 file unit ≈ 0.32 µm (verified: 50 file units ≈ 16 µm pin↔pad
  matching tolerance after master-fp transform).
- 1 mm ≈ 3,125 file units. 1 mil (0.0254 mm) ≈ 79 file units.
- Typical motherboard span: ±1,000,000 file units (≈ 320 mm).
- All coords stored as `(Y, X)` on disk, NOT `(X, Y)`. Every reader
  must swap. Verified across pads, segments, and polylines.
- Origin (0, 0) is anchored on a mounting hole (`MH1` on Gigabyte
  boards). Real CAD trace endpoints never snap to origin — it's a
  reference anchor, not a routing target.

## 2. File macro structure (Z490 — 6.78 MB; B550/X570 similar shape)

| Range            | Size   | Content                                          |
|------------------|--------|--------------------------------------------------|
| 0 – 8.5 K        | 8 KB   | header / signature / fab metadata                |
| 8.5 K – 4.76 M   | 4.75 MB| TOP-layer trace data (Custom_35 region)          |
| 4.76 M – 5.99 M  | 1.23 MB| BOTTOM-layer trace data (Custom_17 region)       |
| 5.99 M – 6.05 M  | 50 KB  | net name table                                   |
| 6.05 M – 6.53 M  | 480 KB | chip records (per-component metadata)            |
| 6.72 M+          | tail   | master footprint section                         |

Two-layer mobos have one TOP and one BOTTOM trace region; multi-layer
boards (e.g. GPU PCBs at 8-12 copper layers) have one trace region per
layer with the same internal structure. Region offsets are auto-detected
by `tvw_topology._autodetect_layer_regions`, which scans Custom_NN
Pascal-prefixed headers and picks any one followed by a >= 50 KB payload
gap. Each detected region is then capped at the net-table start (the
originally-recorded region ends often run into footprint definitions
that produce false matches downstream).

## 3. Custom_NN section markers

Both regions begin with a length-prefixed Pascal string `"Custom_NN"`
that names the section (`Custom_35` for TOP, `Custom_17` for BOTTOM).
Other Custom_NN strings appear throughout the file and identify
record types or sub-sections.

## 4. Pad records

Two coexisting formats. Both end in the same field layout, with the
54-byte format inserting 16 extra bytes between bbox and net_id.

### 38-byte pad (TOP-layer pads, mostly Custom_35)

```
+0..3    uint32  pad shape signature (varies by category)
+4..19   int32×4 bounding box (Y_min, X_min, Y_max, X_max)
+20..21  byte×2  sentinel = 0x00 0x00
+22..25  uint32  net_id        (< 4000 in valid records)
+26..29  uint32  pad_type      (< 100,000)
+30..33  int32   Y world coord (|Y| < 2,000,000)
+34..37  int32   X world coord (|X| < 2,000,000)
```

### 54-byte pad (BOTTOM-layer pads, Custom_17 + through-hole connectors)

```
+0..3    uint32  pad shape signature
+4..19   int32×4 bounding box
+20..23  uint32  layer flag    (often 1)
+24..27  uint32  ?             (often 0)
+28..31  int32   drill diameter / via size (e.g. 716800)
+32..35  int32   second copy of drill (often)
+36..37  byte×2  sentinel = 0x00 0x00
+38..41  uint32  net_id
+42..45  uint32  pad_type
+46..49  int32   Y world coord
+50..53  int32   X world coord
```

The 54-byte format is what holds **through-hole connector bottom-layer
pads** — DDR4 slots, ATX power, etc. Without scanning for it, DDR4_B2
shows 176 pins (not 288), ATX shows 14 (not 24).

### Pad scanner heuristic

Used in `tvw_topology._scan_pads_stride_aware`:
- Slide a window through the trace region
- Match: sentinel `0x00 0x00` at the right offset for each stride,
  net_id < 4000, pad_type < 100,000, |X|,|Y| < 2,000,000
- Accept runs of ≥ 3 consecutive matches (was 50 before 2026-05-07)

## 5. Master footprint section (cracked 2026-05-07)

Located at +6,716,106 in Z490 (similar offset in other boards). One
record per footprint type used on the board:

```
PCIESLOT-164STH   ← length-prefixed footprint name
[outline polygon vertices]
[per-pin point records]
DDR4-288P-STH-... ← next footprint
...
```

Each pin point gives a footprint-local `(lx, ly)` position.

### Cracked transform (footprint-local → world)

```python
# Step 1: swap (lx, ly) → (ly, lx)
# Step 2: rotate by -rotation° CCW
# Step 3: translate by chip world position
swap_x, swap_y = ly, lx
cos_t = cos(-rotation_rad)
sin_t = sin(-rotation_rad)
world_x = chip_x + swap_x * cos_t - swap_y * sin_t
world_y = chip_y + swap_x * sin_t + swap_y * cos_t
```

The file's `rotation` enum is left-handed:
- rot=0   → `(ly, lx)`
- rot=90  → `(lx, -ly)`   (mirror Y)
- rot=180 → `(-ly, -lx)`
- rot=270 → `(-lx, ly)`   (mirror X)

### Verification

91.8% of 32,713 pins across Z490/B550/X570 match an actual pad
within 50 file units (~16 µm) using the unified transform.

### Code

Public API in `tvw_master_fp.py`:
- `parse_master_footprints(buf) → dict[footprint_name → list[(pin_name, lx, ly)]]`
- `pins_world_positions(footprint_name, (chip_x, chip_y), rotation, master_fps)`
  → `list[(pin_idx, world_x, world_y)]`

## 6. Polyline records

Trace polylines are stored in two framings.

### Block-framed polylines

```
[uint32 count][uint32 type=1]
[count polylines, each:]
   (4 zero bytes, except for the first)
   [uint32 K][K × (int32 Y, int32 X)]
```

Block polylines don't carry a per-polyline net_id; net membership
is resolved via shared endpoints with pads/segments.

### Tagged polylines

```
[uint32 net_id][uint32 K][K × (int32 Y, int32 X)][4 zero bytes]
```

`net_id` references the net name table. Found by `find_tagged_polylines_in_gap`
in `tvw_seg_27_unified_v3.py`.

### Polyline chains (X570-specific)

X570 stores polylines as bare chains separated by 4 or 12 zero bytes:

```
[uint32 K][K × (int32 Y, int32 X)]
[4 or 12 zero bytes]
[uint32 K][K × (int32 Y, int32 X)]
...
```

## 7. Segment records (24 bytes)

Trace segments connect two endpoints with a single straight line:

```
+0..3    uint32  net_id   (< 4000; 0 = untagged)
+4..7    uint32  K        (≤ 50 — physical width or layer index)
+8..11   int32   Y1
+12..15  int32   X1
+16..19  int32   Y2
+20..23  int32   X2
```

Found in gaps between pad runs and polyline blocks. Validation
requires `length² ≤ 1e12` (max ~1 mm separation). Runs of ≥ 10
consecutive valid segments are accepted.

## 8. Net name table

Located between the trace data and the chip records.

```
[per-board header]
[net_id][length-prefixed name][...]
```

Boards differ in how net 0 is treated:
- Z490: `N48617361` placeholder, treat 0 as "untagged"
- B550: `VNB_FB+` (real-looking but unused), treat as "untagged"
- X570: `GND` (real and used), treat 0 as a real net

`TraceGraph._zero_is_real_net` is auto-detected from pad density:
if >2 % of pads have net_id=0, it's a real net.

## 9. Chip records (partial)

Located at +6.05 M in Z490. Per-component metadata: refdes, package,
position, rotation. Format includes length-prefixed strings for
device names and footprint names. Used by parser to populate
`BoardModel.components`.

Fully decoded enough for board navigation; not exhaustively
documented here.

## 10. False-match record families

Two record types in the trace region trick the segment/polyline
scanners. Both are defensively filtered in `tvw_topology.py` (cache v4).

### Family A — per-layer aperture/drill tables

5–7 tables per board, one per copper layer (TOP, BOTTOM, INT1, INT2,
VCC, GND).

Header pattern:
```
[length-prefix layer name × 2]    e.g. "\x04INT1\x04INT1"
[length-prefix Z:\CAD\<board>.fab path]
[uint32 N preamble count][N × 8 preamble bytes]
[10–22 records of 24 bytes]
```

24-byte aperture record:
```
struct AperturePadEntry {
    uint32_t aperture_id;     // first row only; 0 elsewhere
    uint32_t count;           // always 1
    int32_t  dim_x;           // diameter in file units (400-23600)
    int32_t  dim_y;           // = dim_x for round, ≠ for oval
    uint32_t shape_type;      // 0=round, 1=oval, 3=?
    uint32_t reserved;        // always 0
};
```

Round entries (shape_type=0, reserved=0) read by the segment scanner
as `(Y2=0, X2=0)` — endpoint at world origin. Dedup collapses them
into MH1's pad node, producing a fan when MH1's net is highlighted.

Sample run boundaries on Z490: 0x309641 (INT2), 0x48a28f (BOTTOM),
0x1ebff7 (INT1), 0x409d75 (VCC), 0x44e845 (GND). Common dim values:
400, 700, 1000, 1500, 2000, 5500, 11800, 18500, 23600 — typical
PCB drill / pad sizes.

### Family D — aperture variant with `01` at the X1 byte position

Same data as Family A but laid out so the segment scanner's `(net_id, K,
Y1, X1, Y2, X2)` alignment hits at offset +8 of an aperture-like record:
the constant `01 00 00 00` in bytes [+12..+15] reads as `X1=1`, with
`Y1` taking the aperture's `dim` value (5900, 7400, etc.). Result:
endpoints at `(1, 5900)`, `(1, 7400)`, `(1, 11800)` — on the Y axis but
not near origin.

These got past the v5 near-origin filter because Y is far from 0.
Caught by v6 axis-epsilon filter (drop if either coord ≤ 10).

### Family C — dimension/footprint records (`(1, 0)` endpoint segments)

Records of 24 bytes each, in 12 distinct runs across Z490 (similar on
X570/B550). Format:
```
struct DimRecord {
    uint32_t flag1 = 1;       // constant
    uint32_t flag0_a = 0;     // constant
    uint32_t flag0_b = 0;     // constant
    uint32_t flag1_b = 1;     // constant
    int32_t  Y;               // varies (13800, 7200, 5300...)
    int32_t  X;               // varies (10200, 2000, 4300...)
};
```

Each record's first 16 bytes are the constant signature
`01 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00`. The trailing 8 bytes
are an `(Y, X)` pair giving a dimension or position.

Often appears immediately after a footprint outline polygon. The
trailing pairs look like per-pin pad sizes — square (4700, 4700) for
some pads, oval (8500, 8700) for others, rectangular (15000, 8500)
for connectors. Could be a per-footprint pin-stack table.

Segment scanner false-matches because the constant prefix reads as
`(net_id=1, K=0, Y1=0, X1=1)` — endpoint at `(1, 0)`, 1 unit from
world origin. That gets dedup'd via the 50-unit spatial-hash tolerance
into whatever chip sits at world (0, 0) — typically a mounting hole.

Sample run boundaries on Z490: 0x21f6 (15 records), 0x309861 (33),
0x40a1a5 (34), 0x44ec75 (34), 0x48ac9c (31), 0x1ec1cf (34), and 6 more.

### Family B — per-chip footprint pin annotations

Records with the recurring `(small_chip_id, pin_count)` marker
followed by per-pin sub-records. Each sub-record is roughly:
```
struct PinAnnotation {
    int32_t  X, Y;            // pin position
    int32_t  small_int;       // 799, 1999... (count or layer)
    float    rotation;        // multiples of 45°
    float    something;       // similar magnitude
    uint32_t footprint_ref;   // 361, 354 — recurring constants
    uint32_t small_int_2;     // 16, 12
};
```

Polyline scanner false-matches because `(chip_id, pin_count)` reads
as `(net_id, K)`. Float rotation bytes (`00 00 87 43` = 270.0) read
as int = 1,132,920,832 — outside ±2M, caught by absurd-vertex filter.

Sample offsets on Z490: 0x394204 (chip 447, 12 pins), 0x394afc
(chip 461, 12 pins). Master-fp already gives correct pin positions
and rotations — these records are redundant for pin↔net mapping.

## 11. Open questions

- **Family B exact stride** — observed sub-record fields fit ~24 bytes
  but the high-level grouping (number of sub-records per chip, header
  size) isn't fully verified.
- **`shape_type=3`** in Family A apertures (last record of every
  table). Likely a special slot/oval/composite shape.
- **Custom_11 through Custom_16, Custom_18 through Custom_34** — many
  Pascal strings appear at the file head but their roles aren't
  documented here.
- **Pad shape signature at +0..3** — known to vary by category
  (round, oval, BGA ball, mounting hole) but no enumeration.
- **Block polyline `count` vs `type` field** — `type=1` is the only
  value observed. Other values may exist in non-Gigabyte TVW files.

## 12. Test files (Gigabyte board variants)

| Board                                | Size    | Pads   | Polylines | Segments |
|--------------------------------------|---------|-------:|----------:|---------:|
| Z490 VISION G r1.0                   | 6.78 MB | 49,914 |     8,420 |   43,171 |
| Gigabyte X570 GAMING X r1.01         | 3.40 MB | 22,432 |     2,008 |   42,369 |
| B550 AORUS PRO AC r1.0               | 5.69 MB | 42,769 |     7,071 |   42,189 |

All three pass `tvw_poly_verify.py` (vertex sanity, no garbage
records) and `tvw_phase3_test.py` (topology build + broken-net
detection + point→net lookup) as of v4.

## 13. Tools

Reading/parsing:
- `tvw_parser.py` — main parser, builds `BoardModel`
- `tvw_master_fp.py` — master footprint cracker
- `tvw_topology.py` — connectivity graph
- `tvw_seg_27_unified_v3.py` — binary scanners (polylines, segments,
  pad runs, polyline chains)

Rendering / dispatch:
- `viewer.py` — Tk app + Skia GL/CPU trace renderer
- `boardview.py` — file-format dispatcher

## 14. Cache versioning

`TraceGraph._CACHE_VERSION` is bumped whenever record counts or graph
structure changes. Mismatched cached pickles trigger a full rebuild.

- v1: initial topology with bbox heuristics
- v2: dataclasses gained `slots=True`
- v3: pad threshold lowered 50→3, region capped at net-table start
- v4: defensive vertex/origin filters in `_extract_layer_records`
- v5: origin filter widened from "exactly (0,0)" to "within 100 file
  units of origin" + extended to pads. Catches Family-A oval/special
  apertures (endpoints `(0,1)` / `(0,3)`) and Family-C dimension records
  (endpoints `(1,0)`)
- v6: filter generalised to "axis epsilon" — drop if either coord is
  within 10 file units of 0. Catches Family-D records (endpoints like
  `(1, 5900)`) which lie on the Y axis but far from origin.
- v7: segment length cap tightened from 1,000,000 (~320 mm — Phase 1
  scanner default) to 500,000 (~160 mm). Real PCB segments top out at
  ~200,000 (~64 mm). Catches Family-B-region byte sequences whose int
  fields happen to satisfy segment validation, producing 240 mm-long
  "traces" that visually cross the whole board.

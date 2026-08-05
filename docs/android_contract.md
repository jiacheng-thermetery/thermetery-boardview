# Android port — module contract (v1)

Three components, three owners, one contract. Nobody edits another owner's
files. Paths are relative to the repo root (`C:/thermetery-boardview`).

| Component | Files | Role |
|---|---|---|
| Python export | `board_export.py` | BoardModel → JSON for the renderer |
| JS renderer | `android/app/src/main/assets/viewer.html`, `viewer.js`, `viewer.css` | canvas rendering + touch UI |
| Android shell | everything else under `android/` (Gradle, Kotlin, manifest) | file open, key dialog, Python embed, WebView host |

Native kernels are already built at `android/app/src/main/jniLibs/<abi>/lib*.so`
(arm64-v8a + x86_64, 16 KB-aligned, NDK r29; see `build_android_native.bat`).

---

## 1. Board JSON schema (the wire format)

All coordinates are board-world units, **y-up** (the renderer flips). Net and
layer references are integer indices into the `nets` / `layers` arrays; `-1`
means unknown/none.

### `open_board` result

```jsonc
{
  "ok": true,
  "version": 1,
  "meta": {
    "title": "Gigabyte_X570_GAMING_X_REV1.01",   // display name (file stem)
    "format": "tvw",          // tvw | tvw-compal | fz | xzzpcb | gencad | brd | asc
    "warnings": ["..."],      // BoardModel.warnings if present, else []
    "units_per_mm": null,     // float when known, else null
    "bbox": [minx, miny, maxx, maxy],   // overall board bounds from pins+outlines
    "traces_available": true  // BoardModel.topology_available
  },
  "layers": ["TOP", "BOTTOM"],   // render layers known so far (indices stable)
  "nets": ["GND", "+3V3", "..."],
  "components": [
    {
      "ref": "U1",
      "x": 0.0, "y": 0.0,          // component origin (Component.x/y)
      "layer": 0,                   // index into layers (TOP=0, BOTTOM=1 always)
      "rotation": 0.0,              // degrees, as stored
      "bbox": [minx, miny, maxx, maxy],   // absolute, transform baked in
      "outline": [[x, y], ...],     // absolute polygon, or null (use bbox)
      "pins": [
        {"name": "1", "x": 1.23, "y": 4.56, "net": 17}   // ABSOLUTE coords
      ]
    }
  ]
}
```

**Pin coordinates are absolute** — the exporter bakes in the component
position/rotation/mirror transform exactly the way `viewer.py` does for
hit-testing (study `viewer.py`'s component/pin transform before writing it;
the shape's pins are relative offsets in `Shape.pins`).

### Failure result (any function)

```jsonc
{"ok": false, "error": "key_required", "reason": "missing",  "format": "fz"}
{"ok": false, "error": "key_required", "reason": "invalid",  "format": "xzzpcb"}
{"ok": false, "error": "parse_error",  "reason": "<message>", "format": "?"}
```

`key_required` maps from `fz_parser.FZKeyError.reason` and the XZZ
`model.key_required` convention — see `viewer.py:_load_with_key_prompt` for
the existing retry semantics the Kotlin shell replicates.

### `load_traces` result

```jsonc
{
  "ok": true,
  "layers": ["TOP", "BOTTOM", "In1", "..."],  // REPLACES the layer list (superset)
  "segments": {                // flat parallel arrays, one entry per segment
    "x1": [], "y1": [], "x2": [], "y2": [],
    "layer": [],               // index into layers above
    "net": [],                 // index into nets from open_board, -1 unknown
    "width": []                // world units; 0 = hairline
  },
  "vias": { "x": [], "y": [], "net": [] }     // may be empty arrays
}
```

Synthetic ratsnest topologies (GENCAD/BRD/FZ/XZZ) export their airwires as
segments too — mark them with `"synthetic": true` at the top level so the
renderer draws them dashed.

---

## 2. Python module: `board_export.py`

Module-level functions (called from Kotlin via Chaquopy; module keeps the
current board in a module-global):

```python
def open_board(path: str, key: str | None = None) -> str:   # JSON per §1
def load_traces() -> str:                                    # JSON per §1
def ping() -> str:    # '{"ok": true, "native": {"tvw": true, "xzz": true, "rc6": true}}'
def validate_key(fmt: str, key_text: str) -> str  # "ok" | human-readable problem;
                                                  # shared with the desktop key
                                                  # manager via src/key_store.py
```

- Returns JSON **strings** (never dicts) — one `json.dumps`, compact separators.
- Never raises across the bridge: catch everything, return the failure shape.
- `open_board` must NOT build topology (keep first paint fast);
  `load_traces` triggers the build (TVW: seconds — the shell calls it off
  the UI thread).
- `ping()` reports whether each native kernel loaded — the shell logs it at
  startup (it proves the jniLibs + bare-soname loader path works on-device).

## 3. JS global: `window.bv` (renderer exposes)

```js
bv.onBoard(json)    // string or object; replaces current board, fits view
bv.onTraces(json)   // attaches segments/vias, replaces layer list
bv.onStatus(text)   // status line ("Parsing…", "Building topology…", "")
bv.onError(text)    // toast/banner, auto-dismiss
```

## 4. Kotlin bridge: `window.Android` (shell injects via addJavascriptInterface)

```js
Android.openFilePicker()  // SAF picker; shell parses; later calls bv.onBoard(...)
Android.loadTraces()      // async; shell later calls bv.onTraces(...)
Android.openKeyManager()  // launches KeyManagerActivity (per-format key screen)
Android.log(msg)          // logcat passthrough (tag "BoardviewJS")
```

**Renderer dev-harness rule:** when `window.Android` is undefined (desktop
browser), the renderer must show its own file input + drag-drop that loads a
board JSON file directly, and auto-fetch `./sample.json` if present. This is
how the renderer is developed and tested without Android.

## 5. Renderer v1 feature scope

- Pan (one-finger drag / mouse drag), pinch zoom (+ wheel zoom on desktop),
  double-tap to zoom in, "fit" button. Maintain zoom-to-focal-point math.
- Tap: select component (nearest within dp-scaled radius; prefer pins when
  zoomed in). Selection shows an info panel: ref, pin count, and pin→net
  list; tapping a pin/net name highlights that net (all pins on it + its
  segments in bright color, everything else dimmed).
- Layer cycle button (TOP/BOTTOM/inner when present): components on other
  layers ghosted; segments filtered to layer, selected net always shown on
  all layers.
- View rotation: two toolbar buttons rotate the view in 90-degree steps
  (state threaded through fit/hit-test/rasterize).
- Traces toggle button: first press calls `Android.loadTraces()` (or loads
  from the dev-harness JSON if it already has segments).
- Search box with prefix autocomplete over refdes and nets (datalist is fine).
- Render perf: pre-render static layers to offscreen canvas(es) at current
  zoom bucket; composite + transform during gestures; re-rasterize on gesture
  end. Must stay interactive with 50k segments + 20k pins (typical desktop
  board), on a phone.
- Colors: dark background, Thermetery-ish palette; selected net bright
  yellow/cyan; synthetic ratsnest dashed.

## 6. Android shell v1 feature scope

- Single activity, fullscreen WebView (assets served from
  `file:///android_asset/viewer.html` or WebViewAssetLoader — either is fine,
  JS needs no network). `addJavascriptInterface(bridge, "Android")`.
- SAF `ACTION_OPEN_DOCUMENT` (+ manifest intent filters for
  `.tvw/.fz/.pcb/.cad/.brd/.brd2/.bv` opened from file managers). Copy the
  content stream to `cacheDir/boards/<displayName>` — **keep it for the whole
  session** (lazy topology re-reads the path later; do not delete after parse).
- Python startup in `Application.onCreate` on a background thread
  (`Python.start`), parse calls on a single background executor (Python is
  effectively single-threaded under the GIL).
- On `key_required`: native dialog with multi-line input, message naming the
  format (ASUS .fz = 44 hex words / XZZ .pcb = 16 hex digits), retry up to 3
  times (mirror `viewer.py:_load_with_key_prompt`), optional "remember on this
  device" → `filesDir/keys/<fmt>.txt`; pass remembered key automatically on
  next `open_board` failure of that format. `android:allowBackup="false"`.
- Chaquopy: Python 3.13, `pip { install "numpy==1.26.2" }`, abiFilters
  arm64-v8a + x86_64. Python sources staged by a Gradle copy task
  (`:app:stagePythonSources`) from the repo root, **preserving the
  `src/` package paths** (the parser core uses relative imports and
  cannot be flattened). The authoritative module list lives in
  `android/app/build.gradle.kts` (`pythonRootModules` +
  `pythonPackageModules`): `board_export.py` at the staged root plus
  the `src/` package (`__init__.py, ratsnest.py, runtime_paths.py,
  key_store.py, units.py, tvw_compal.py, tvw_topology.py`) and
  `src/parsers/` (`__init__.py, boardview.py, gencad_parser.py,
  brd_parser.py, asc_parser.py, tvw_parser.py, tvw_master_fp.py,
  tvw_trace_scanners.py, fz_parser.py, xzzpcb_parser.py,
  tvw_native.py, xzz_native.py`). The Gradle task fails the build if a
  listed file is missing — when a module moves or a new import is
  added to the core, update the list in the same change. (NEVER glob
  the repo root — it contains gitignored sample boards and Windows
  DLLs.)
- minSdk 24, targetSdk 35, versionName from repo git describe if easy, else
  hardcode `0.1.0`.
- Local toolchain on this machine: JDK `C:\Android\jdk-17.0.19+10`, Gradle
  `C:\Android\gradle-8.10.2\bin\gradle.bat`, SDK `C:\Android\sdk`
  (build-tools 34/35, platform android-35). Chaquopy 17 requires
  buildPython's minor version to match the target (3.13):
  `C:/Users/Administrator/AppData/Local/Python/pythoncore-3.13-64/python.exe`
  (the older anaconda 3.11 does not work). No Gradle wrapper needed —
  invoke the local Gradle directly with `-p android`.

## 7. What v1 explicitly does NOT include

Measurement tool, walker/diagnostics, .fz decrypted-text cache (disabled on
Android — native RC6 makes it unnecessary), Play Store packaging, x86 32-bit.

Also not supported: opening eM-Test Expert `.asc` sets from the picker.
The format is a *directory* of sibling files, but the shell copies a
single SAF stream to `cacheDir/boards/`, so the parser cannot find the
siblings. `asc_parser.py` still ships in the APK (boardview.py imports
it unconditionally); on-device support would need a folder picker +
multi-file copy.

# Web-Based Canvas Core (Android WebView Renderer)

`viewer.js` (with `viewer.html` / `viewer.css`, under
`app/src/main/assets/`) implements the complete interactive boardview
experience that runs inside the Android WebView. It is deliberately
self-contained: zero network or external-library dependencies, works
from `file:///android_asset`.

## Architecture overview

1. **Ingest** — `window.bv.onBoard` / `onTraces` receive JSON payloads
   from the Kotlin host (produced by `board_export.py`). The data is
   normalised into typed arrays and spatial indices once.
2. **Static raster** — All non-interactive geometry (substrate,
   segments, vias, pins, component outlines, refdes labels) is drawn
   into an off-screen canvas at the settled view transform. During
   pan/pinch the raster is blitted with a delta transform.
3. **Live overlay** — Only the current selection (component outline +
   pin marker + label) is redrawn per frame on top of the blit.
4. **Settle timer** — 150 ms after the last gesture the static raster
   is recomputed so subsequent interaction stays crisp.

This separation keeps interaction at frame rate on large boards while
preserving pixel-perfect fidelity once the view settles.

## Key properties

- **Coordinate fidelity** — world y-up to screen y-down conversion,
  rotation about the viewport centre, and zoom/pan maths match the
  desktop viewer's visual results.
- **Hit-testing** — x-sorted pin index + binary search for the
  x-window, then Euclidean distance. Component hit uses outline
  containment (bbox for tiny parts). Pin resolution is gated by
  on-screen pitch so dense BGA balls don't steal taps from the package
  body.
- **Layer & net highlighting** — dim-alpha for everything else while a
  net is highlighted; bright passes for the selected net's
  pins/segments/vias. Palette matches the desktop viewer.
- **Memory discipline** — typed arrays (`Float64Array`, `Int32Array`)
  for geometry; label budget capped at 400 entries; raster size capped
  at 14 Mpixels to prevent runaway memory on high-DPR devices.
- **Contract compliance** — implements the surface defined in
  `docs/android_contract.md` (`window.bv` callbacks, `window.Android`
  bridge methods, status/toast/error channels).

## Performance model

| Phase | Cost |
|-------|------|
| Board ingest + index build | O(N log N) once (pin sort) |
| Rasterize | O(segments + pins + comps) at settle |
| Gesture frame | O(1) blit + O(1) selection |
| Tap hit-test | O(log N + K), K = pins in the x-window |

## Integration notes

- The host (Kotlin) injects board/traces JSON via `evaluateJavascript`
  calling `window.bv.onBoard` / `onTraces`.
- Key-manager and file-picker actions are delegated to the Android
  bridge (`window.Android.openFilePicker/openKeyManager`).
- Desktop development is supported by opening the same file directly in
  a browser (drag-and-drop + sample.json auto-load).

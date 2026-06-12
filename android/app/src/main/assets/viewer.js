/* Thermetery boardview — Android WebView canvas renderer.
 *
 * Contract: docs/android_contract.md (§1 schema, §3 window.bv, §4 window.Android, §5 scope).
 * No external/network dependencies; runs from file:///android_asset offline.
 *
 * Renders board JSON onto a single canvas sized to devicePixelRatio.
 * Heavy static geometry (segments, vias, pins) is pre-rendered into an
 * offscreen raster at the settled view; during gestures the raster is
 * blitted with a delta transform and re-rasterized ~150 ms after the
 * gesture settles. Component outlines / refdes labels draw live above.
 */
"use strict";
(function () {

  /* ------------------------------------------------------------------ *
   *  Palette (matches viewer.py)
   * ------------------------------------------------------------------ */
  var BG             = "#0d1024";
  var BOARD_FILL     = "#11142e";
  var BOARD_EDGE     = "#232a4e";
  var TOP_COLOR      = "#5b8fff";
  var BOTTOM_COLOR   = "#ff6b5b";
  var GHOST_OUTLINE  = "#2a3052";
  var SELECTED_OUTLINE = "#22ddee";
  var PIN_COLOR      = "#ffff88";
  var SELECTED_PIN_COLOR = "#ff3399";
  var SELECTED_PIN_RING  = "#ffffff";
  var HIGHLIGHT      = "#ffe45b";   // highlighted net pins
  var TRACE_HIGHLIGHT = "#ffff66";  // highlighted net segments
  var VIA_COLOR      = "#00ccff";
  var LABEL_TOP      = "#9fb6ff";
  var LABEL_BOTTOM   = "#ffaa9f";
  var LABEL_SELECTED = "#aaffff";

  // (bright, dim) per layer name; inner layers cycle through INNER_PALETTE.
  var LAYER_COLORS = {
    "TOP":    ["#5b8fff", "#1c2c50"],
    "BOTTOM": ["#ff6b5b", "#3a1c14"]
  };
  var INNER_PALETTE = [
    ["#5bff8f", "#1c5025"],   // green
    ["#bf5bff", "#350c4d"],   // purple
    ["#5bffe1", "#0c4d44"],   // cyan
    ["#ffaa5b", "#4d2d10"],   // orange
    ["#ff5bbf", "#4d0c35"],   // pink
    ["#bfff5b", "#3a4d0c"]    // lime
  ];

  var DIM_ALPHA   = 0.35;   // everything-else alpha while a net is highlighted
  var GHOST_ALPHA = 0.22;   // other-layer component ghosting
  var LABEL_MIN_PX = 18;    // desktop viewer's 18 px refdes rule
  var TAP_RADIUS_CSS = 24;  // pin hit radius (CSS px)
  var PIN_RESOLVE_PX = 12;  // pin pitch on screen must exceed this to pick pins
  var SETTLE_MS = 150;      // re-rasterize this long after the gesture settles
  var DASH = [6, 5];        // synthetic ratsnest dash pattern

  /* ------------------------------------------------------------------ *
   *  DOM
   * ------------------------------------------------------------------ */
  var canvas   = document.getElementById("bv-canvas");
  var ctx      = canvas.getContext("2d");
  var elStatus = document.getElementById("bv-status");
  var elToast  = document.getElementById("bv-toast");
  var elPanel  = document.getElementById("bv-panel");
  var elPanelRef  = document.getElementById("bv-panel-ref");
  var elPanelMeta = document.getElementById("bv-panel-meta");
  var elPanelPins = document.getElementById("bv-panel-pins");
  var elChip      = document.getElementById("bv-netchip");
  var elChipName  = document.getElementById("bv-netchip-name");
  var elSearch    = document.getElementById("bv-search");
  var elSuggest   = document.getElementById("bv-suggest");
  var btnOpen   = document.getElementById("bv-btn-open");
  var btnLayer  = document.getElementById("bv-btn-layer");
  var btnTraces = document.getElementById("bv-btn-traces");
  var btnFit    = document.getElementById("bv-btn-fit");
  var elErrlog  = document.getElementById("bv-errlog");
  var elDev     = document.getElementById("bv-dev");
  var elDevFile = document.getElementById("bv-dev-file");
  var elDropHint = document.getElementById("bv-drophint");

  var hasAndroid = (typeof window.Android !== "undefined" && window.Android !== null);

  function alog(msg) {
    try {
      if (hasAndroid && window.Android.log) window.Android.log(String(msg));
    } catch (e) { /* ignore */ }
  }

  /* ------------------------------------------------------------------ *
   *  State
   * ------------------------------------------------------------------ */
  var board = null;        // normalized board (see ingestBoard)
  var traces = null;       // {x1,y1,x2,y2,layer,net,width (typed), n, viaX,viaY,viaNet, synthetic}
  var tracesLoaded = false;
  var tracesPending = false;
  var showTraces = false;

  var layerIdx = -1;       // -1 = ALL, else index into board.layers
  var hlNet = -1;          // highlighted net index, -1 = none
  var sel = null;          // {ci, pin}  pin = -1 when whole component

  var view = { k: 1, tx: 0, ty: 0 };
  var cssW = 0, cssH = 0, dpr = Math.max(1, window.devicePixelRatio || 1);

  var dirty = false, rafPending = false;
  var needRaster = false;
  var gestureActive = false;
  var settleTimer = 0, wheelTimer = 0;

  // offscreen static raster
  var raster = {
    canvas: document.createElement("canvas"),
    ctx: null,
    valid: false,
    k: 1, tx: 0, ty: 0,    // view at capture time
    ox: 0, oy: 0,          // capture-screen CSS coords of the raster's top-left
    dpr: 1,
    cssWdt: 0, cssHgt: 0
  };
  raster.ctx = raster.canvas.getContext("2d");

  /* ------------------------------------------------------------------ *
   *  View transform   (world y-up -> screen y-down)
   *    sx = wx * k + tx        sy = -wy * k + ty
   * ------------------------------------------------------------------ */
  function worldX(sx) { return (sx - view.tx) / view.k; }
  function worldY(sy) { return (view.ty - sy) / view.k; }

  function clampK(k) {
    if (!board) return k;
    return Math.min(board.maxK, Math.max(board.minK, k));
  }

  function applyZoomAt(fx, fy, factor) {
    if (!board) return;
    var wx = worldX(fx), wy = worldY(fy);
    var k2 = clampK(view.k * factor);
    view.k = k2;
    view.tx = fx - wx * k2;
    view.ty = fy + wy * k2;
    scheduleRender();
  }

  function fitInsets() {
    return { t: 56, r: 12, b: 34, l: 12 };
  }

  function fitView() {
    if (!board) return;
    var bb = board.bbox;
    var bw = Math.max(bb[2] - bb[0], 1e-9);
    var bh = Math.max(bb[3] - bb[1], 1e-9);
    var ins = fitInsets();
    var aw = Math.max(40, cssW - ins.l - ins.r);
    var ah = Math.max(40, cssH - ins.t - ins.b);
    var k = Math.min(aw / bw, ah / bh);
    view.k = clampK(k);
    var cx = (bb[0] + bb[2]) / 2, cy = (bb[1] + bb[3]) / 2;
    view.tx = ins.l + aw / 2 - cx * view.k;
    view.ty = ins.t + ah / 2 + cy * view.k;
    needRaster = true;
    scheduleRender();
  }

  function centerOn(wx, wy, k) {
    if (k) view.k = clampK(k);
    var scy = elPanel.classList.contains("open") ? cssH * 0.38 : cssH * 0.5;
    view.tx = cssW / 2 - wx * view.k;
    view.ty = scy + wy * view.k;
    needRaster = true;
    scheduleRender();
  }

  /* ------------------------------------------------------------------ *
   *  Layer colors
   * ------------------------------------------------------------------ */
  var layerBright = [], layerDim = [];

  function rebuildLayerColors() {
    layerBright.length = 0;
    layerDim.length = 0;
    if (!board) return;
    var inner = 0;
    for (var i = 0; i < board.layers.length; i++) {
      var name = String(board.layers[i]).toUpperCase();
      var pair = LAYER_COLORS[name];
      if (!pair) { pair = INNER_PALETTE[inner % INNER_PALETTE.length]; inner++; }
      layerBright.push(pair[0]);
      layerDim.push(pair[1]);
    }
  }

  /* ------------------------------------------------------------------ *
   *  Board ingestion
   * ------------------------------------------------------------------ */
  function parseMaybe(json) {
    if (typeof json === "string") {
      try { return JSON.parse(json); }
      catch (e) { showToast("Bad JSON: " + e.message); return null; }
    }
    return json;
  }

  function failureText(obj) {
    if (obj.error === "key_required") {
      return "Key required (" + (obj.reason || "missing") + ") for ." +
             (obj.format || "?") + " file";
    }
    if (obj.error === "parse_error") {
      return "Parse error: " + (obj.reason || "unknown");
    }
    return "Error: " + (obj.error || "unknown") + " — " + (obj.reason || "");
  }

  function ingestBoard(obj) {
    var meta = obj.meta || {};
    var comps = obj.components || [];
    var layers = (obj.layers && obj.layers.length) ? obj.layers.slice() : ["TOP", "BOTTOM"];
    var nets = obj.nets || [];

    // bbox: meta.bbox, else from component bboxes/pins
    var bb = meta.bbox;
    if (!bb || bb.length !== 4 || !isFinite(bb[0])) {
      bb = [Infinity, Infinity, -Infinity, -Infinity];
      for (var i = 0; i < comps.length; i++) {
        var c = comps[i];
        if (c.bbox && isFinite(c.bbox[0])) {
          if (c.bbox[0] < bb[0]) bb[0] = c.bbox[0];
          if (c.bbox[1] < bb[1]) bb[1] = c.bbox[1];
          if (c.bbox[2] > bb[2]) bb[2] = c.bbox[2];
          if (c.bbox[3] > bb[3]) bb[3] = c.bbox[3];
        }
        var pins = c.pins || [];
        for (var j = 0; j < pins.length; j++) {
          var p = pins[j];
          if (p.x < bb[0]) bb[0] = p.x;
          if (p.y < bb[1]) bb[1] = p.y;
          if (p.x > bb[2]) bb[2] = p.x;
          if (p.y > bb[3]) bb[3] = p.y;
        }
      }
      if (!isFinite(bb[0])) bb = [0, 0, 1, 1];
    }

    // flat pin arrays for fast raster + hit-test passes
    var nPins = 0;
    for (i = 0; i < comps.length; i++) nPins += (comps[i].pins || []).length;
    var pinX = new Float64Array(nPins), pinY = new Float64Array(nPins);
    var pinNet = new Int32Array(nPins), pinComp = new Int32Array(nPins);
    var k = 0;
    for (i = 0; i < comps.length; i++) {
      var cc = comps[i];
      var pp = cc.pins || [];
      cc.pinStart = k;
      cc.pinCount = pp.length;
      // ensure a bbox exists for every component
      if (!cc.bbox || cc.bbox.length !== 4 || !isFinite(cc.bbox[0])) {
        var cbb = [Infinity, Infinity, -Infinity, -Infinity];
        for (j = 0; j < pp.length; j++) {
          if (pp[j].x < cbb[0]) cbb[0] = pp[j].x;
          if (pp[j].y < cbb[1]) cbb[1] = pp[j].y;
          if (pp[j].x > cbb[2]) cbb[2] = pp[j].x;
          if (pp[j].y > cbb[3]) cbb[3] = pp[j].y;
        }
        if (!isFinite(cbb[0])) cbb = [cc.x || 0, cc.y || 0, (cc.x || 0), (cc.y || 0)];
        cc.bbox = cbb;
      }
      for (j = 0; j < pp.length; j++) {
        pinX[k] = pp[j].x;
        pinY[k] = pp[j].y;
        pinNet[k] = (typeof pp[j].net === "number") ? pp[j].net : -1;
        pinComp[k] = i;
        k++;
      }
    }

    var b = {
      meta: meta,
      title: meta.title || "board",
      layers: layers,
      nets: nets,
      comps: comps,
      bbox: bb,
      nPins: nPins,
      pinX: pinX, pinY: pinY, pinNet: pinNet, pinComp: pinComp,
      diag: Math.hypot(bb[2] - bb[0], bb[3] - bb[1]) || 1,
      synthetic: !!obj.synthetic
    };

    // per-component pin pitch (min pairwise distance over a small sample)
    var pitches = [];
    for (i = 0; i < comps.length; i++) {
      var pc = compPitchCompute(b, comps[i]);
      comps[i].pitch = pc;
      if (pc > 0) pitches.push(pc);
    }
    pitches.sort(function (a, bx) { return a - bx; });
    b.pitch = pitches.length ? pitches[pitches.length >> 1] : b.diag / 200;
    for (i = 0; i < comps.length; i++) {
      if (!(comps[i].pitch > 0)) comps[i].pitch = b.pitch;
    }

    // zoom clamps relative to the fitted scale
    var ins = fitInsets();
    var fk = Math.min(
      Math.max(40, cssW - ins.l - ins.r) / Math.max(bb[2] - bb[0], 1e-9),
      Math.max(40, cssH - ins.t - ins.b) / Math.max(bb[3] - bb[1], 1e-9));
    if (!isFinite(fk) || fk <= 0) fk = 1;
    b.minK = fk * 0.05;
    b.maxK = fk * 5000;
    return b;
  }

  function compPitchCompute(b, c) {
    var n = c.pinCount;
    if (n < 2) return 0;
    var stride = Math.max(1, Math.floor(n / 24));
    var xs = [], ys = [];
    for (var i = 0; i < n && xs.length < 24; i += stride) {
      xs.push(b.pinX[c.pinStart + i]);
      ys.push(b.pinY[c.pinStart + i]);
    }
    var best = Infinity;
    for (i = 0; i < xs.length; i++) {
      for (var j = i + 1; j < xs.length; j++) {
        var dx = xs[i] - xs[j], dy = ys[i] - ys[j];
        var d2 = dx * dx + dy * dy;
        if (d2 > 1e-18 && d2 < best) best = d2;
      }
    }
    return isFinite(best) ? Math.sqrt(best) : 0;
  }

  /* ------------------------------------------------------------------ *
   *  Traces ingestion
   * ------------------------------------------------------------------ */
  function ingestTraces(obj) {
    var s = obj.segments || {};
    var n = (s.x1 || []).length;
    var t = {
      n: n,
      x1: new Float64Array(s.x1 || []),
      y1: new Float64Array(s.y1 || []),
      x2: new Float64Array(s.x2 || []),
      y2: new Float64Array(s.y2 || []),
      layer: new Int32Array(s.layer || []),
      net: new Int32Array(s.net || []),
      width: new Float64Array(s.width || []),
      synthetic: !!obj.synthetic
    };
    var v = obj.vias || {};
    t.viaX = new Float64Array(v.x || []);
    t.viaY = new Float64Array(v.y || []);
    t.viaNet = new Int32Array(v.net || []);
    t.nVias = t.viaX.length;
    return t;
  }

  /* ------------------------------------------------------------------ *
   *  window.bv — the contract API (§3)
   * ------------------------------------------------------------------ */
  window.bv = {
    onBoard: function (json) {
      var obj = parseMaybe(json);
      if (!obj) return;
      if (obj.ok === false) { showToast(failureText(obj)); return; }
      try {
        board = ingestBoard(obj);
      } catch (e) {
        showToast("Board load failed: " + e.message);
        alog("ingestBoard: " + (e.stack || e));
        return;
      }
      traces = null;
      tracesLoaded = false;
      tracesPending = false;
      showTraces = false;
      hlNet = -1;
      sel = null;
      layerIdx = -1;
      closePanel();
      updateChip();
      // dev-harness JSON may carry segments inline (board+traces in one file)
      if (obj.segments && obj.segments.x1 && obj.segments.x1.length) {
        try {
          traces = ingestTraces(obj);
          tracesLoaded = true;
        } catch (e2) { alog("inline traces ignored: " + e2.message); }
      }
      rebuildLayerColors();
      rebuildSearchIndex();
      updateButtons();
      fitView();
      var nc = board.comps.length, nn = board.nets.length;
      var warn = (board.meta.warnings && board.meta.warnings.length)
        ? "  ·  " + board.meta.warnings.length + " warning(s)" : "";
      setStatus(board.title + "  ·  " + nc + " comps  ·  " + nn + " nets" + warn);
      if (board.meta.warnings && board.meta.warnings.length) {
        alog("board warnings: " + board.meta.warnings.join(" | "));
      }
      maybeRunDevTest();
    },

    onTraces: function (json) {
      var obj = parseMaybe(json);
      if (!obj) { tracesPending = false; return; }
      if (obj.ok === false) {
        tracesPending = false;
        showToast(failureText(obj));
        updateButtons();
        return;
      }
      if (!board) return;
      try {
        traces = ingestTraces(obj);
      } catch (e) {
        tracesPending = false;
        showToast("Traces load failed: " + e.message);
        return;
      }
      // layer list is REPLACED by a superset; existing indices stay stable
      if (obj.layers && obj.layers.length >= board.layers.length) {
        board.layers = obj.layers.slice();
        rebuildLayerColors();
      }
      tracesLoaded = true;
      tracesPending = false;
      showTraces = true;
      updateButtons();
      needRaster = true;
      scheduleRender();
    },

    onStatus: function (text) {
      setStatus(text == null ? "" : String(text));
    },

    onError: function (text) {
      showToast(text == null ? "error" : String(text));
    }
  };

  /* ------------------------------------------------------------------ *
   *  Status line + toast
   * ------------------------------------------------------------------ */
  function setStatus(text) {
    elStatus.textContent = text || "";
  }

  var toastTimer = 0;
  function showToast(text) {
    elToast.textContent = text;
    elToast.classList.remove("hidden");
    elToast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      elToast.classList.remove("show");
    }, 4000);
    alog("toast: " + text);
  }

  /* ------------------------------------------------------------------ *
   *  Canvas sizing
   * ------------------------------------------------------------------ */
  function resizeCanvas() {
    dpr = Math.max(1, window.devicePixelRatio || 1);
    cssW = window.innerWidth;
    cssH = window.innerHeight;
    var w = Math.max(1, Math.round(cssW * dpr));
    var h = Math.max(1, Math.round(cssH * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    needRaster = true;
    scheduleRender();
  }

  /* ------------------------------------------------------------------ *
   *  Offscreen raster of static geometry (segments + vias + pins)
   * ------------------------------------------------------------------ */
  function rasterize() {
    if (!board || cssW < 2 || cssH < 2) { raster.valid = false; return; }
    var marginX = cssW * 0.5, marginY = cssH * 0.5;
    var rw = cssW + 2 * marginX, rh = cssH + 2 * marginY;
    var rDpr = Math.min(dpr, 2);
    var maxPix = 14e6;
    if (rw * rDpr * rh * rDpr > maxPix) {
      rDpr = Math.sqrt(maxPix / (rw * rh));
    }
    var pw = Math.max(1, Math.round(rw * rDpr));
    var ph = Math.max(1, Math.round(rh * rDpr));
    if (raster.canvas.width !== pw || raster.canvas.height !== ph) {
      raster.canvas.width = pw;
      raster.canvas.height = ph;
    }
    raster.k = view.k; raster.tx = view.tx; raster.ty = view.ty;
    raster.ox = -marginX; raster.oy = -marginY;
    raster.dpr = rDpr;
    raster.cssWdt = rw; raster.cssHgt = rh;

    var g = raster.ctx;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, pw, ph);
    // map capture-screen CSS coords -> raster pixels
    g.setTransform(rDpr, 0, 0, rDpr, marginX * rDpr, marginY * rDpr);

    var k = view.k, tx = view.tx, ty = view.ty;
    var L = -marginX, T = -marginY, R = cssW + marginX, B = cssH + marginY;

    // --- board substrate ---
    var bb = board.bbox;
    var bx = bb[0] * k + tx, by = -bb[3] * k + ty;
    var bw = (bb[2] - bb[0]) * k, bh = (bb[3] - bb[1]) * k;
    g.fillStyle = BOARD_FILL;
    g.fillRect(bx, by, bw, bh);
    g.strokeStyle = BOARD_EDGE;
    g.lineWidth = 1;
    g.strokeRect(bx, by, bw, bh);

    var dimming = (hlNet >= 0);

    // --- segments (traces / ratsnest), batched by (layer, width bucket) ---
    if (traces && showTraces && traces.n) {
      var t = traces;
      var batches = {};            // key -> [i, i, ...]
      var hlIdx = [];
      for (var i = 0; i < t.n; i++) {
        var li = t.layer[i];
        var isHl = dimming && t.net[i] === hlNet;
        if (isHl) { hlIdx.push(i); continue; }     // highlighted: all layers
        if (layerIdx >= 0 && li !== layerIdx) continue;
        // cull
        var ax = t.x1[i] * k + tx, ay = -t.y1[i] * k + ty;
        var bx2 = t.x2[i] * k + tx, by2 = -t.y2[i] * k + ty;
        if ((ax < L && bx2 < L) || (ax > R && bx2 > R) ||
            (ay < T && by2 < T) || (ay > B && by2 > B)) continue;
        var wpx = t.width[i] * k;
        if (wpx < 0.8) wpx = 0.8;
        if (wpx > 20) wpx = 20;
        var bucket = Math.round(wpx * 2);          // 0.5 px quantization
        var key = li * 64 + bucket;
        (batches[key] || (batches[key] = [])).push(i);
      }
      g.lineCap = "round";
      if (t.synthetic) g.setLineDash(DASH);
      g.globalAlpha = dimming ? DIM_ALPHA : 0.8;
      for (var key2 in batches) {
        var idxs = batches[key2];
        var kn = key2 | 0;
        var lay = (kn / 64) | 0;
        g.strokeStyle = layerBright[lay] || "#888899";
        g.lineWidth = (kn % 64) / 2;
        g.beginPath();
        for (var q = 0; q < idxs.length; q++) {
          var s = idxs[q];
          g.moveTo(t.x1[s] * k + tx, -t.y1[s] * k + ty);
          g.lineTo(t.x2[s] * k + tx, -t.y2[s] * k + ty);
        }
        g.stroke();
      }
      g.globalAlpha = 1;
      // highlighted net segments: bright, on every layer, drawn above
      if (hlIdx.length) {
        g.strokeStyle = TRACE_HIGHLIGHT;
        g.beginPath();
        var hw = 0;
        for (q = 0; q < hlIdx.length; q++) {
          s = hlIdx[q];
          var w2 = t.width[s] * k * 1.2;
          if (w2 > hw) hw = w2;
          g.moveTo(t.x1[s] * k + tx, -t.y1[s] * k + ty);
          g.lineTo(t.x2[s] * k + tx, -t.y2[s] * k + ty);
        }
        g.lineWidth = Math.max(2, Math.min(hw, 20));
        g.stroke();
      }
      g.setLineDash([]);

      // --- vias ---
      if (t.nVias) {
        var vr = Math.max(1.2, Math.min(board.pitch * 0.25 * k, 5));
        g.fillStyle = VIA_COLOR;
        g.globalAlpha = dimming ? DIM_ALPHA : 0.9;
        for (i = 0; i < t.nVias; i++) {
          if (dimming && t.viaNet[i] === hlNet) continue;
          var vx = t.viaX[i] * k + tx, vy = -t.viaY[i] * k + ty;
          if (vx < L || vx > R || vy < T || vy > B) continue;
          g.beginPath();
          g.arc(vx, vy, vr, 0, 6.2832);
          g.fill();
        }
        g.globalAlpha = 1;
        if (dimming) {
          g.fillStyle = HIGHLIGHT;
          for (i = 0; i < t.nVias; i++) {
            if (t.viaNet[i] !== hlNet) continue;
            vx = t.viaX[i] * k + tx; vy = -t.viaY[i] * k + ty;
            if (vx < L || vx > R || vy < T || vy > B) continue;
            g.beginPath();
            g.arc(vx, vy, vr + 1, 0, 6.2832);
            g.fill();
          }
        }
      }
    }

    // --- pins ---
    var comps = board.comps;
    var px = board.pinX, py = board.pinY, pn = board.pinNet, pcArr = board.pinComp;
    g.fillStyle = PIN_COLOR;
    var lastComp = -1, r = 1, ghost = false, alpha = -1;
    for (i = 0; i < board.nPins; i++) {
      var ci = pcArr[i];
      if (ci !== lastComp) {
        lastComp = ci;
        var c = comps[ci];
        r = c.pitch * 0.27 * k;
        if (r < 0.6) r = 0.6;
        if (r > 8) r = 8;
        ghost = (layerIdx >= 0 && c.layer !== layerIdx);
      }
      if (dimming && pn[i] === hlNet) continue;    // bright pass below
      var sx = px[i] * k + tx, sy = -py[i] * k + ty;
      if (sx < L || sx > R || sy < T || sy > B) continue;
      var a = ghost ? 0.15 : 0.85;
      if (dimming) a *= DIM_ALPHA;
      if (a !== alpha) { g.globalAlpha = a; alpha = a; }
      if (r < 1.4) {
        g.fillRect(sx - r, sy - r, r + r, r + r);
      } else {
        g.beginPath();
        g.arc(sx, sy, r, 0, 6.2832);
        g.fill();
      }
    }
    g.globalAlpha = 1;
    // highlighted net pins: bright on all layers
    if (dimming) {
      g.fillStyle = HIGHLIGHT;
      lastComp = -1; r = 1;
      for (i = 0; i < board.nPins; i++) {
        if (pn[i] !== hlNet) continue;
        ci = pcArr[i];
        if (ci !== lastComp) {
          lastComp = ci;
          r = comps[ci].pitch * 0.3 * k;
          if (r < 1.4) r = 1.4;
          if (r > 9) r = 9;
        }
        sx = px[i] * k + tx; sy = -py[i] * k + ty;
        if (sx < L || sx > R || sy < T || sy > B) continue;
        g.beginPath();
        g.arc(sx, sy, r, 0, 6.2832);
        g.fill();
      }
    }

    raster.valid = true;
    needRaster = false;
  }

  /* ------------------------------------------------------------------ *
   *  Render loop — runs only when dirty
   * ------------------------------------------------------------------ */
  function scheduleRender() {
    dirty = true;
    if (!rafPending) {
      rafPending = true;
      requestAnimationFrame(frame);
    }
  }

  function frame() {
    rafPending = false;
    if (!dirty) return;
    dirty = false;

    if (needRaster && !gestureActive) rasterize();

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = BG;
    ctx.fillRect(0, 0, cssW, cssH);

    if (!board) {
      ctx.fillStyle = "#3a4170";
      ctx.font = "16px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(hasAndroid ? "Open a board file" : "Open or drop a board JSON",
                   cssW / 2, cssH / 2);
      ctx.textAlign = "left";
      return;
    }

    // blit static raster with the delta transform
    if (raster.valid) {
      var K = view.k / raster.k;
      var ox = (raster.ox - raster.tx) * K + view.tx;
      var oy = (raster.oy - raster.ty) * K + view.ty;
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(raster.canvas, ox, oy,
                    raster.canvas.width * K / raster.dpr,
                    raster.canvas.height * K / raster.dpr);
    }

    drawComponents();
    drawSelection();
  }

  function drawComponents() {
    var comps = board.comps;
    var k = view.k, tx = view.tx, ty = view.ty;
    var n = comps.length;
    var dimming = (hlNet >= 0);
    var skipLabels = gestureActive && n > 3500;
    var labelBudget = 400;
    var lastFont = 0;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 1;

    for (var i = 0; i < n; i++) {
      var c = comps[i];
      var bb = c.bbox;
      var x = bb[0] * k + tx, y = -bb[3] * k + ty;
      var w = (bb[2] - bb[0]) * k, h = (bb[3] - bb[1]) * k;
      if (x > cssW || y > cssH || x + w < 0 || y + h < 0) continue;
      var sizePx = w > h ? w : h;
      if (sizePx < 2.5) continue;
      var ghost = (layerIdx >= 0 && c.layer !== layerIdx);
      var alpha = ghost ? GHOST_ALPHA : 1;
      if (dimming) alpha *= DIM_ALPHA;
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = ghost ? GHOST_OUTLINE
                       : (c.layer === 1 ? BOTTOM_COLOR : TOP_COLOR);
      var ol = c.outline;
      if (ol && ol.length > 2 && sizePx > 6) {
        ctx.beginPath();
        ctx.moveTo(ol[0][0] * k + tx, -ol[0][1] * k + ty);
        for (var j = 1; j < ol.length; j++) {
          ctx.lineTo(ol[j][0] * k + tx, -ol[j][1] * k + ty);
        }
        ctx.closePath();
        ctx.stroke();
      } else {
        ctx.strokeRect(x, y, w, h);
      }
      // refdes label: only when the component exceeds the 18 px rule
      if (!ghost && !skipLabels && labelBudget > 0 && sizePx >= LABEL_MIN_PX) {
        var fs = sizePx * 0.18;
        if (fs < 9) fs = 9;
        if (fs > 12) fs = 12;
        fs = fs | 0;
        if (fs !== lastFont) {
          ctx.font = "bold " + fs + "px Consolas, monospace";
          lastFont = fs;
        }
        ctx.fillStyle = (c.layer === 1 ? LABEL_BOTTOM : LABEL_TOP);
        ctx.fillText(c.ref, x + w / 2, y + h / 2);
        labelBudget--;
      }
    }
    ctx.globalAlpha = 1;
    ctx.textAlign = "left";
  }

  function drawSelection() {
    if (!sel || !board) return;
    var c = board.comps[sel.ci];
    if (!c) return;
    var k = view.k, tx = view.tx, ty = view.ty;
    var bb = c.bbox;
    var x = bb[0] * k + tx, y = -bb[3] * k + ty;
    var w = (bb[2] - bb[0]) * k, h = (bb[3] - bb[1]) * k;
    ctx.strokeStyle = SELECTED_OUTLINE;
    ctx.lineWidth = 2;
    var ol = c.outline;
    if (ol && ol.length > 2) {
      ctx.beginPath();
      ctx.moveTo(ol[0][0] * k + tx, -ol[0][1] * k + ty);
      for (var j = 1; j < ol.length; j++) {
        ctx.lineTo(ol[j][0] * k + tx, -ol[j][1] * k + ty);
      }
      ctx.closePath();
      ctx.stroke();
    } else {
      ctx.strokeRect(x - 1, y - 1, w + 2, h + 2);
    }
    // selected refdes label, always visible
    ctx.font = "bold 12px Consolas, monospace";
    ctx.fillStyle = LABEL_SELECTED;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.fillText(c.ref, x + w / 2, y - 4);
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    // selected pin marker
    if (sel.pin >= 0 && sel.pin < c.pinCount) {
      var pi = c.pinStart + sel.pin;
      var sx = board.pinX[pi] * k + tx, sy = -board.pinY[pi] * k + ty;
      var r = Math.max(3, Math.min(c.pitch * 0.32 * k, 10));
      ctx.fillStyle = SELECTED_PIN_COLOR;
      ctx.beginPath();
      ctx.arc(sx, sy, r, 0, 6.2832);
      ctx.fill();
      ctx.strokeStyle = SELECTED_PIN_RING;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(sx, sy, r + 1.5, 0, 6.2832);
      ctx.stroke();
    }
    ctx.lineWidth = 1;
  }

  /* ------------------------------------------------------------------ *
   *  Hit-testing
   * ------------------------------------------------------------------ */
  function pointInPoly(wx, wy, poly) {
    var inside = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var xi = poly[i][0], yi = poly[i][1];
      var xj = poly[j][0], yj = poly[j][1];
      if ((yi > wy) !== (yj > wy) &&
          wx < (xj - xi) * (wy - yi) / (yj - yi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }

  function hitTest(sx, sy) {
    var wx = worldX(sx), wy = worldY(sy);
    var radW = TAP_RADIUS_CSS / view.k;
    var rad2 = radW * radW;

    // nearest pin within the tap radius
    var bestPin = -1, bestD2 = rad2;
    var px = board.pinX, py = board.pinY;
    for (var i = 0; i < board.nPins; i++) {
      var dx = px[i] - wx, dy = py[i] - wy;
      var d2 = dx * dx + dy * dy;
      if (d2 < bestD2) { bestD2 = d2; bestPin = i; }
    }
    if (bestPin >= 0) {
      var pci = board.pinComp[bestPin];
      var pc = board.comps[pci];
      // pins win only when their spacing is resolvable on screen
      if (pc.pitch * view.k >= PIN_RESOLVE_PX) {
        return { ci: pci, pin: bestPin - pc.pinStart };
      }
    }

    // component bbox / outline containment, smallest area wins
    var bestCi = -1, bestArea = Infinity;
    var comps = board.comps;
    for (i = 0; i < comps.length; i++) {
      var bb = comps[i].bbox;
      if (wx < bb[0] - radW || wx > bb[2] + radW ||
          wy < bb[1] - radW || wy > bb[3] + radW) continue;
      var inside;
      if (wx >= bb[0] && wx <= bb[2] && wy >= bb[1] && wy <= bb[3]) {
        inside = comps[i].outline && comps[i].outline.length > 2
          ? pointInPoly(wx, wy, comps[i].outline) ||
            // outline may be thin; accept bbox hit when poly misses narrowly
            ((bb[2] - bb[0]) * view.k < 30 && (bb[3] - bb[1]) * view.k < 30)
          : true;
      } else {
        // near-miss: only for small components (finger-friendly)
        inside = ((bb[2] - bb[0]) * view.k < 30 && (bb[3] - bb[1]) * view.k < 30);
      }
      if (inside) {
        var area = (bb[2] - bb[0]) * (bb[3] - bb[1]);
        if (area < bestArea) { bestArea = area; bestCi = i; }
      }
    }
    if (bestCi >= 0) {
      var pin = -1;
      if (bestPin >= 0 && board.pinComp[bestPin] === bestCi) {
        pin = bestPin - comps[bestCi].pinStart;
      }
      return { ci: bestCi, pin: pin };
    }
    if (bestPin >= 0) {
      // not pin-resolvable, but the pin still identifies a component
      return { ci: board.pinComp[bestPin], pin: -1 };
    }
    return null;
  }

  /* ------------------------------------------------------------------ *
   *  Selection panel + net highlight
   * ------------------------------------------------------------------ */
  var MAX_PIN_ROWS = 1500;

  function netName(idx) {
    if (idx < 0 || !board || idx >= board.nets.length) return null;
    return board.nets[idx];
  }

  function selectComp(ci, pin, center) {
    sel = { ci: ci, pin: (pin == null ? -1 : pin) };
    var c = board.comps[ci];
    elPanelRef.textContent = c.ref;
    var lay = board.layers[c.layer] || ("L" + c.layer);
    elPanelMeta.textContent = c.pinCount + " pin" + (c.pinCount === 1 ? "" : "s") +
      "  ·  " + lay +
      (c.rotation ? "  ·  " + c.rotation + "°" : "");
    buildPinRows(c);
    elPanel.classList.add("open");
    if (center) {
      var bb = c.bbox;
      var maxDim = Math.max(bb[2] - bb[0], bb[3] - bb[1], board.pitch * 2);
      var kTarget = Math.max(view.k, 120 / maxDim);
      centerOn((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2, clampK(kTarget));
    }
    scheduleRender();
  }

  function buildPinRows(c) {
    elPanelPins.textContent = "";
    var frag = document.createDocumentFragment();
    var nRows = Math.min(c.pinCount, MAX_PIN_ROWS);
    for (var i = 0; i < nRows; i++) {
      var pi = c.pinStart + i;
      var net = board.pinNet[pi];
      var row = document.createElement("div");
      row.className = "bv-pinrow";
      if (sel && sel.pin === i) row.className += " sel";
      if (net >= 0 && net === hlNet) row.className += " hl";
      row.dataset.pin = i;
      var name = document.createElement("span");
      name.className = "bv-pinname";
      name.textContent = c.pins[i].name != null ? String(c.pins[i].name) : String(i + 1);
      var netEl = document.createElement("span");
      netEl.className = "bv-pinnet" + (net < 0 ? " none" : "");
      netEl.textContent = net >= 0 ? (netName(net) || ("net " + net)) : "—";
      row.appendChild(name);
      row.appendChild(netEl);
      frag.appendChild(row);
    }
    if (c.pinCount > nRows) {
      var more = document.createElement("div");
      more.className = "bv-pinrow";
      more.textContent = "… " + (c.pinCount - nRows) + " more pins";
      frag.appendChild(more);
    }
    elPanelPins.appendChild(frag);
  }

  elPanelPins.addEventListener("click", function (e) {
    var row = e.target.closest ? e.target.closest(".bv-pinrow") : null;
    if (!row || row.dataset.pin == null || !sel) return;
    var i = row.dataset.pin | 0;
    var c = board.comps[sel.ci];
    sel.pin = i;
    var net = board.pinNet[c.pinStart + i];
    if (net >= 0) {
      setHighlight(net === hlNet ? -1 : net);   // tap again to clear
    }
    buildPinRows(c);
    scheduleRender();
  });

  document.getElementById("bv-panel-close").addEventListener("click", function () {
    sel = null;
    closePanel();
    scheduleRender();
  });

  function closePanel() {
    elPanel.classList.remove("open");
  }

  function setHighlight(net) {
    if (net === hlNet) return;
    hlNet = net;
    updateChip();
    if (sel) buildPinRows(board.comps[sel.ci]);
    needRaster = true;
    scheduleRender();
  }

  function updateChip() {
    if (hlNet >= 0 && board) {
      elChipName.textContent = netName(hlNet) || ("net " + hlNet);
      elChip.classList.remove("hidden");
    } else {
      elChip.classList.add("hidden");
    }
  }

  document.getElementById("bv-netchip-clear").addEventListener("click", function () {
    setHighlight(-1);
  });

  function centerNet(net) {
    var minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    for (var i = 0; i < board.nPins; i++) {
      if (board.pinNet[i] !== net) continue;
      var x = board.pinX[i], y = board.pinY[i];
      if (x < minx) minx = x;
      if (y < miny) miny = y;
      if (x > maxx) maxx = x;
      if (y > maxy) maxy = y;
    }
    if (traces) {
      for (i = 0; i < traces.n; i++) {
        if (traces.net[i] !== net) continue;
        if (traces.x1[i] < minx) minx = traces.x1[i];
        if (traces.y1[i] < miny) miny = traces.y1[i];
        if (traces.x1[i] > maxx) maxx = traces.x1[i];
        if (traces.y1[i] > maxy) maxy = traces.y1[i];
        if (traces.x2[i] < minx) minx = traces.x2[i];
        if (traces.y2[i] < miny) miny = traces.y2[i];
        if (traces.x2[i] > maxx) maxx = traces.x2[i];
        if (traces.y2[i] > maxy) maxy = traces.y2[i];
      }
    }
    if (!isFinite(minx)) return;
    var bw = maxx - minx, bh = maxy - miny;
    var k = view.k;
    if (bw > 1e-9 || bh > 1e-9) {
      k = clampK(Math.min(cssW / Math.max(bw, 1e-9),
                          cssH / Math.max(bh, 1e-9)) * 0.6);
      if (k > view.k && view.k * Math.max(bw, bh) > 80) k = view.k; // already visible
    }
    centerOn((minx + maxx) / 2, (miny + maxy) / 2, k);
  }

  /* ------------------------------------------------------------------ *
   *  Pointer input: pan / pinch / tap / double-tap
   * ------------------------------------------------------------------ */
  var pointers = new Map();
  var gesture = null;            // {type:'pan'|'pinch', ...}
  var lastTapT = 0, lastTapX = 0, lastTapY = 0;

  function endGestureSoon() {
    gestureActive = false;
    clearTimeout(settleTimer);
    settleTimer = setTimeout(function () {
      needRaster = true;
      scheduleRender();
    }, SETTLE_MS);
  }

  canvas.addEventListener("pointerdown", function (e) {
    e.preventDefault();
    try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* ok */ }
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    clearTimeout(settleTimer);
    if (pointers.size === 1) {
      gesture = {
        type: "pan", id: e.pointerId,
        sx: e.clientX, sy: e.clientY,
        tx0: view.tx, ty0: view.ty,
        moved: false, t0: performance.now()
      };
    } else if (pointers.size === 2) {
      var pts = [];
      pointers.forEach(function (p) { pts.push(p); });
      var cx = (pts[0].x + pts[1].x) / 2, cy = (pts[0].y + pts[1].y) / 2;
      var d0 = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      gesture = {
        type: "pinch",
        wfx: worldX(cx), wfy: worldY(cy),
        k0: view.k, d0: d0
      };
      gestureActive = true;
    }
  });

  canvas.addEventListener("pointermove", function (e) {
    var p = pointers.get(e.pointerId);
    if (!p) return;
    p.x = e.clientX;
    p.y = e.clientY;
    if (!gesture || !board) return;
    if (gesture.type === "pinch" && pointers.size >= 2) {
      var pts = [];
      pointers.forEach(function (q) { pts.push(q); });
      var cx = (pts[0].x + pts[1].x) / 2, cy = (pts[0].y + pts[1].y) / 2;
      var d = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      var k2 = clampK(gesture.k0 * d / gesture.d0);
      view.k = k2;
      view.tx = cx - gesture.wfx * k2;
      view.ty = cy + gesture.wfy * k2;
      scheduleRender();
    } else if (gesture.type === "pan" && e.pointerId === gesture.id) {
      var dx = e.clientX - gesture.sx, dy = e.clientY - gesture.sy;
      if (!gesture.moved && dx * dx + dy * dy > 64) {
        gesture.moved = true;
        gestureActive = true;
      }
      if (gesture.moved) {
        view.tx = gesture.tx0 + dx;
        view.ty = gesture.ty0 + dy;
        scheduleRender();
      }
    }
  });

  function pointerEnd(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    if (gesture && gesture.type === "pinch") {
      if (pointers.size === 1) {
        // continue as a pan with the remaining finger
        var rem = null, remId = -1;
        pointers.forEach(function (p, id) { rem = p; remId = id; });
        gesture = {
          type: "pan", id: remId,
          sx: rem.x, sy: rem.y,
          tx0: view.tx, ty0: view.ty,
          moved: true, t0: performance.now()
        };
        return;
      }
    } else if (gesture && gesture.type === "pan" && e.pointerId === gesture.id) {
      var wasTap = !gesture.moved &&
                   (performance.now() - gesture.t0) < 400 &&
                   e.type !== "pointercancel";
      if (wasTap) handleTap(e.clientX, e.clientY);
    }
    if (pointers.size === 0) {
      gesture = null;
      if (gestureActive) endGestureSoon();
    }
  }
  canvas.addEventListener("pointerup", pointerEnd);
  canvas.addEventListener("pointercancel", pointerEnd);

  function handleTap(x, y) {
    if (!board) return;
    var now = performance.now();
    var dx = x - lastTapX, dy = y - lastTapY;
    if (now - lastTapT < 320 && dx * dx + dy * dy < 1600) {
      // double-tap: zoom 2x toward the tap point
      lastTapT = 0;
      applyZoomAt(x, y, 2);
      needRaster = true;
      clearTimeout(settleTimer);
      settleTimer = setTimeout(function () { scheduleRender(); }, SETTLE_MS);
      return;
    }
    lastTapT = now;
    lastTapX = x;
    lastTapY = y;
    var hit = hitTest(x, y);
    if (hit) {
      selectComp(hit.ci, hit.pin, false);
    } else {
      sel = null;
      closePanel();
      scheduleRender();
    }
  }

  // wheel zoom for desktop testing
  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    if (!board) return;
    var dy = e.deltaY;
    if (e.deltaMode === 1) dy *= 16;
    applyZoomAt(e.clientX, e.clientY, Math.exp(-dy * 0.0015));
    gestureActive = true;
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(function () {
      gestureActive = false;
      needRaster = true;
      scheduleRender();
    }, SETTLE_MS);
  }, { passive: false });

  /* ------------------------------------------------------------------ *
   *  Toolbar
   * ------------------------------------------------------------------ */
  btnOpen.addEventListener("click", function () {
    if (hasAndroid && window.Android.openFilePicker) {
      window.Android.openFilePicker();
    } else {
      elDevFile.click();
    }
  });

  btnFit.addEventListener("click", function () {
    fitView();
  });

  btnLayer.addEventListener("click", function () {
    if (!board) return;
    layerIdx = (layerIdx + 2) % (board.layers.length + 1) - 1;  // -1,0,1,...,n-1,-1
    updateButtons();
    needRaster = true;
    scheduleRender();
  });

  btnTraces.addEventListener("click", function () {
    if (!board) return;
    if (!tracesLoaded) {
      if (board.meta && board.meta.traces_available === false) {
        showToast("No trace data available in this file");
        return;
      }
      if (tracesPending) return;
      if (hasAndroid && window.Android.loadTraces) {
        tracesPending = true;
        setStatus("Loading traces…");
        window.Android.loadTraces();
      } else {
        showToast("No trace data loaded (drop a traces JSON)");
      }
      return;
    }
    showTraces = !showTraces;
    updateButtons();
    needRaster = true;
    scheduleRender();
  });

  function updateButtons() {
    if (layerIdx < 0) {
      btnLayer.textContent = "ALL";
      btnLayer.classList.remove("on");
    } else {
      var name = board ? String(board.layers[layerIdx]) : "?";
      btnLayer.textContent = name.length > 7 ? name.slice(0, 7) : name;
      btnLayer.classList.add("on");
    }
    btnTraces.classList.toggle("on", showTraces);
  }

  /* ------------------------------------------------------------------ *
   *  Search (datalist prefix autocomplete over refdes + nets)
   * ------------------------------------------------------------------ */
  var searchIndex = [];   // {key, label, type:'comp'|'net', idx}

  function rebuildSearchIndex() {
    searchIndex.length = 0;
    if (!board) return;
    for (var i = 0; i < board.comps.length; i++) {
      var ref = board.comps[i].ref;
      if (ref) searchIndex.push({ key: String(ref).toLowerCase(),
                                  label: String(ref), type: "comp", idx: i });
    }
    for (i = 0; i < board.nets.length; i++) {
      var nm = board.nets[i];
      if (nm) searchIndex.push({ key: String(nm).toLowerCase(),
                                 label: String(nm), type: "net", idx: i });
    }
  }

  elSearch.addEventListener("input", function () {
    var v = elSearch.value.trim().toLowerCase();
    elSuggest.textContent = "";
    if (!v) return;
    var frag = document.createDocumentFragment();
    var count = 0;
    for (var i = 0; i < searchIndex.length && count < 50; i++) {
      if (searchIndex[i].key.lastIndexOf(v, 0) === 0) {    // startsWith
        var opt = document.createElement("option");
        opt.value = searchIndex[i].label;
        opt.label = searchIndex[i].type;
        frag.appendChild(opt);
        count++;
      }
    }
    elSuggest.appendChild(frag);
  });

  function searchActivate(value) {
    var v = String(value || "").trim().toLowerCase();
    if (!v || !board) return;
    var exact = null, prefix = null;
    for (var i = 0; i < searchIndex.length; i++) {
      var ent = searchIndex[i];
      if (ent.key === v) {
        if (!exact || (exact.type === "net" && ent.type === "comp")) exact = ent;
      } else if (!prefix && ent.key.lastIndexOf(v, 0) === 0) {
        prefix = ent;
      }
    }
    var hitEnt = exact || prefix;
    if (!hitEnt) { showToast("No match: " + value); return; }
    if (hitEnt.type === "comp") {
      selectComp(hitEnt.idx, -1, true);
    } else {
      setHighlight(hitEnt.idx);
      centerNet(hitEnt.idx);
    }
    elSearch.blur();
  }

  elSearch.addEventListener("change", function () {
    if (elSearch.value) searchActivate(elSearch.value);
  });
  elSearch.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      searchActivate(elSearch.value);
    }
  });

  /* ------------------------------------------------------------------ *
   *  Dev harness — desktop browser only (window.Android undefined)
   * ------------------------------------------------------------------ */
  function devLoadText(text) {
    var obj = parseMaybe(text);
    if (!obj) return;
    if (!obj.components && obj.segments) {
      window.bv.onTraces(obj);     // a bare load_traces result
    } else {
      window.bv.onBoard(obj);
    }
  }

  function initDevHarness() {
    elDev.classList.remove("hidden");

    elDevFile.addEventListener("change", function () {
      var f = elDevFile.files && elDevFile.files[0];
      if (!f) return;
      var rd = new FileReader();
      rd.onload = function () { devLoadText(rd.result); };
      rd.readAsText(f);
      elDevFile.value = "";
    });

    window.addEventListener("dragover", function (e) {
      e.preventDefault();
      elDropHint.classList.remove("hidden");
    });
    window.addEventListener("dragleave", function (e) {
      if (e.relatedTarget == null) elDropHint.classList.add("hidden");
    });
    window.addEventListener("drop", function (e) {
      e.preventDefault();
      elDropHint.classList.add("hidden");
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      var rd = new FileReader();
      rd.onload = function () { devLoadText(rd.result); };
      rd.readAsText(f);
    });

    // on-page error overlay (dev only)
    window.addEventListener("error", function (e) {
      elErrlog.classList.remove("hidden");
      elErrlog.textContent += "[error] " + e.message +
        (e.filename ? "  (" + e.filename + ":" + e.lineno + ")" : "") + "\n";
    });
    window.addEventListener("unhandledrejection", function (e) {
      elErrlog.classList.remove("hidden");
      elErrlog.textContent += "[promise] " + (e.reason && e.reason.message || e.reason) + "\n";
    });

    // silently try a sibling sample.json (dev convenience; never shipped)
    try {
      fetch("./sample.json").then(function (r) {
        return r.ok ? r.text() : null;
      }).then(function (txt) {
        if (txt && !board) devLoadText(txt);
      }).catch(function () { /* no sample — fine */ });
    } catch (e) { /* file:// fetch may throw — fine */ }
  }

  // scripted states for headless screenshot testing:
  //   viewer.html?test=select  — select biggest comp + highlight one of its nets
  //   viewer.html?test=layer   — cycle to BOTTOM layer with traces shown
  function maybeRunDevTest() {
    if (hasAndroid) return;
    var m = /[?&]test=(\w+)/.exec(location.search);
    if (!m) return;
    if (m[1] === "layer") {
      setTimeout(function () {
        if (!board) return;
        layerIdx = Math.min(1, board.layers.length - 1);
        if (tracesLoaded) showTraces = true;
        updateButtons();
        needRaster = true;
        scheduleRender();
      }, 120);
      return;
    }
    if (m[1] !== "select") return;
    setTimeout(function () {
      if (!board || !board.comps.length) return;
      var best = 0;
      for (var i = 1; i < board.comps.length; i++) {
        if (board.comps[i].pinCount > board.comps[best].pinCount) best = i;
      }
      selectComp(best, 0, true);
      var c = board.comps[best];
      for (i = 0; i < c.pinCount; i++) {
        var net = board.pinNet[c.pinStart + i];
        if (net >= 0) { setHighlight(net); break; }
      }
      if (tracesLoaded) { showTraces = true; updateButtons(); needRaster = true; }
      scheduleRender();
    }, 120);
  }

  /* ------------------------------------------------------------------ *
   *  Boot
   * ------------------------------------------------------------------ */
  window.addEventListener("resize", resizeCanvas);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", resizeCanvas);
  }
  window.addEventListener("error", function (e) {
    alog("js error: " + e.message + " @" + e.filename + ":" + e.lineno);
  });

  if (!hasAndroid) initDevHarness();
  resizeCanvas();
  scheduleRender();
  alog("viewer.js ready (dpr=" + dpr + ", android=" + hasAndroid + ")");
})();

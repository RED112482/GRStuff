const state = {
  slots: [],
  wallSlots: [],
  selectedElevation: null,
  anchorScan: null,
  field: "reflectivity",
  rangeKm: 150,
  smooth: true,
  scanHistory: [],
  scanIndex: 0,
  wallMode: false,
  followLatest: true,
  boundaries: null,
  boundariesLoading: false,
  lastStatus: null,
  lastLiveToken: null,
  scanSequence: [],
  scanStatus: {},
  wallTiles: [],
  view: {scale: 1, x: 0, y: 0},
  pointer: null,
  point: null,
  hoverRangeKm: null,
  hoverAzimuth: null,
};

const FIELD_OPTIONS = [
  ["reflectivity", "Reflectivity", "dBZ"],
  ["velocity", "Velocity", "m/s"],
  ["differential_reflectivity", "ZDR", "dB"],
  ["cross_correlation_ratio", "CC", ""],
  ["differential_phase", "PhiDP", "deg"],
  ["spectrum_width", "Spectrum Width", "m/s"],
];

const el = (id) => document.getElementById(id);
const fieldSelect = el("fieldSelect");
const sweepSelect = el("sweepSelect");
const rangeSelect = el("rangeSelect");
const smoothToggle = el("smoothToggle");
const radarImage = el("radarImage");
const radarStage = el("radarStage");
const cursorMarker = el("cursorMarker");

function toast(message) {
  const node = el("toast");
  node.textContent = message;
  node.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add("hidden"), 2400);
}

async function json(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error((await response.text()) || response.statusText);
  }
  return response.json();
}

function fmtUtc(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toISOString().slice(11, 19) + "Z";
}

function sameElevation(a, b) {
  return a != null && b != null &&
    Math.abs(Number(a) - Number(b)) <= 0.06;
}

function scanAgeLabel(scan) {
  if (!scan) return "NO SCAN";
  if (scan.source === "live") {
    if (scan.kind === "BASE") return "LIVE";
    return `${scan.kind} #${scan.sequence_number || 1}`;
  }
  if (Number(scan.volume_offset) === 1) return "PREV VOL";
  return `-${scan.volume_offset || "?"} VOL`;
}

function scanTimestamp(scan) {
  return fmtUtc(scan?.scan_time || scan?.volume_time);
}

function scanUrl(scan, size = 640) {
  if (!scan) return "";
  const params = new URLSearchParams({
    source: scan.source,
    sequence_index: String(scan.sequence_index),
    range_km: String(state.rangeKm),
    smooth: state.smooth ? "true" : "false",
    size: String(size),
  });
  if (scan.archive_key) params.set("archive_key", scan.archive_key);
  return `/api/scan-image/${encodeURIComponent(state.field)}.png?${params.toString()}`;
}

function imageKey(scan, size) {
  return [
    scan?.id || "none",
    state.field,
    state.rangeKm,
    state.smooth ? 1 : 0,
    size,
  ].join("|");
}

function setLiveBadge(mode, text) {
  const badge = el("liveBadge");
  badge.classList.remove("live", "history", "offline");
  if (mode) badge.classList.add(mode);
  el("liveText").textContent = text;
}

function populateFields() {
  fieldSelect.innerHTML = "";
  for (const [id, label] of FIELD_OPTIONS) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = label;
    fieldSelect.appendChild(option);
  }
  fieldSelect.value = state.field;
}

function selectedSlotIndex() {
  return state.slots.findIndex((slot) =>
    sameElevation(slot.elevation, state.selectedElevation)
  );
}

function currentAnchorScan() {
  return state.anchorScan;
}

function updateTimeUI() {
  const scan = currentAnchorScan();
  el("timePastBtn").disabled =
    !scan || state.scanIndex >= state.scanHistory.length - 1;
  el("timeFutureBtn").disabled =
    !scan || state.scanIndex <= 0;
  el("goLiveBtn").classList.toggle("active", state.followLatest);

  if (!scan) {
    el("timeLabel").textContent = "NO SCAN";
    el("timeStamp").textContent = "—";
    return;
  }

  el("timeLabel").textContent =
    state.followLatest ? scanAgeLabel(scan) : `-${state.scanIndex} SCAN`;
  el("timeStamp").textContent = scanTimestamp(scan);
}

function updateStatus(data) {
  state.lastStatus = data;
  const dot = el("statusDot");
  dot.classList.toggle("ok", !!data.loaded);
  dot.classList.toggle("bad", !data.loaded);

  if (data.live_available) {
    setLiveBadge("live", data.live_complete ? "LIVE · COMPLETE" : "LIVE · SCANNING");
    el("statusText").textContent = "AWS Level-II chunk stream";
    el("volumeTime").textContent =
      `${fmtUtc(data.live_volume_time)} · ${data.live_chunk_count || 0} chunks`;
  } else if (data.archive_loaded) {
    setLiveBadge("history", "ARCHIVE");
    el("statusText").textContent = "Latest completed Level-II";
    el("volumeTime").textContent = fmtUtc(data.archive_volume_time);
  } else {
    setLiveBadge("offline", "OFFLINE");
    el("statusText").textContent = "Waiting for radar data";
    el("volumeTime").textContent = data.live_error || data.error || "No data";
  }
}

function renderScanSequence() {
  const container = el("scanSequence");
  container.innerHTML = "";

  state.scanSequence.forEach((scan, index) => {
    const chip = document.createElement("div");
    chip.className =
      "scan-chip " + String(scan.kind || "BASE").toLowerCase();
    if (
      index < state.scanSequence.length - 1 ||
      Number(scan.completion ?? 100) >= 99.5
    ) {
      chip.classList.add("complete");
    }
    if (
      index === state.scanSequence.length - 1 &&
      state.lastStatus?.live_available
    ) {
      chip.classList.add("current");
    }
    chip.textContent = scan.label || Number(scan.elevation).toFixed(2) + "°";
    container.appendChild(chip);
  });

  const next = state.scanStatus?.expected_next;
  if (next) {
    const chip = document.createElement("div");
    chip.className =
      "scan-chip expected " + String(next.kind || "BASE").toLowerCase();
    chip.textContent = "NEXT · " + next.label;
    container.appendChild(chip);
  }

  if (!container.children.length) {
    const chip = document.createElement("div");
    chip.className = "scan-chip";
    chip.textContent = "Waiting for live scan sequence…";
    container.appendChild(chip);
  }

  const now = state.scanStatus?.current;
  el("scanNowLabel").textContent = now
    ? "NOW: " + now.label +
      (Number(now.completion ?? 100) < 99.5
        ? ` · ${Math.round(now.completion)}%`
        : "")
    : "Waiting for scan metadata…";
  el("scanNextLabel").textContent = next ? "NEXT: " + next.label : "";

  requestAnimationFrame(() => {
    container.scrollLeft = container.scrollWidth;
  });
}

function fillElevationSelect() {
  const prior = state.selectedElevation;
  sweepSelect.innerHTML = "";

  state.slots.forEach((slot, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = Number(slot.elevation).toFixed(2) + "°";
    sweepSelect.appendChild(option);
  });

  if (!state.slots.length) {
    state.selectedElevation = null;
    return;
  }

  let selectedIndex = 0;
  if (prior != null) {
    let best = Infinity;
    state.slots.forEach((slot, index) => {
      const delta = Math.abs(Number(slot.elevation) - Number(prior));
      if (delta < best) {
        best = delta;
        selectedIndex = index;
      }
    });
  } else if (state.anchorScan) {
    const exact = state.slots.findIndex((slot) =>
      sameElevation(slot.elevation, state.anchorScan.elevation)
    );
    if (exact >= 0) selectedIndex = exact;
  }

  state.selectedElevation = Number(state.slots[selectedIndex].elevation);
  sweepSelect.value = String(selectedIndex);
}

async function loadScanHistory(preferredScanId = null, newest = false) {
  if (state.selectedElevation == null) return;

  const data = await json(
    "/api/scan-history?elevation=" +
    encodeURIComponent(Number(state.selectedElevation).toFixed(2)) +
    "&limit=12"
  );
  state.scanHistory = data.scans || [];

  if (!state.scanHistory.length) {
    state.scanIndex = 0;
    state.anchorScan = null;
    updateTimeUI();
    return;
  }

  if (newest) {
    state.scanIndex = 0;
  } else if (preferredScanId) {
    const found = state.scanHistory.findIndex((scan) => scan.id === preferredScanId);
    state.scanIndex = found >= 0
      ? found
      : Math.min(state.scanIndex, state.scanHistory.length - 1);
  } else {
    state.scanIndex = Math.min(state.scanIndex, state.scanHistory.length - 1);
  }

  state.anchorScan = state.scanHistory[state.scanIndex];
  updateTimeUI();
}

async function resolveWall(anchor = state.anchorScan) {
  if (!anchor) {
    state.wallSlots = state.slots.map((slot) => ({
      index: slot.index,
      elevation: slot.elevation,
      scan: slot.latest || null,
    }));
    renderAll();
    return;
  }

  const params = new URLSearchParams({
    anchor_source: anchor.source,
    anchor_sequence_index: String(anchor.sequence_index),
  });
  if (anchor.archive_key) {
    params.set("anchor_archive_key", anchor.archive_key);
  }

  const data = await json("/api/wall-state?" + params.toString());
  state.wallSlots = data.slots || [];
  renderAll();
}

async function loadElevations({preserveAnchor = true} = {}) {
  const oldAnchorId = preserveAnchor ? state.anchorScan?.id : null;
  const data = await json("/api/elevations");

  state.slots = data.slots || [];
  state.scanSequence = data.scan_sequence || [];
  state.scanStatus = data.scan_status || {};
  if (!state.anchorScan || !preserveAnchor) {
    state.anchorScan = data.anchor || null;
  }

  fillElevationSelect();
  renderScanSequence();

  let preferredId = oldAnchorId;
  if (!preferredId && state.anchorScan &&
      sameElevation(state.anchorScan.elevation, state.selectedElevation)) {
    preferredId = state.anchorScan.id;
  }

  await loadScanHistory(preferredId, !preserveAnchor);

  if (!preserveAnchor && state.scanHistory.length) {
    state.anchorScan = state.scanHistory[0];
    state.scanIndex = 0;
  }

  await resolveWall(state.anchorScan);
}

async function loadStatus() {
  try {
    const data = await json("/api/status");
    const tokenChanged = data.live_token !== state.lastLiveToken;
    state.lastLiveToken = data.live_token;
    updateStatus(data);

    if (tokenChanged && state.followLatest) {
      await loadElevations({preserveAnchor: false});
    }
  } catch (err) {
    setLiveBadge("offline", "OFFLINE");
    el("statusText").textContent = "Backend unavailable";
    el("volumeTime").textContent = err.message;
  }
}

function beamHeightArlKm(horizontalRangeKm, elevationDeg) {
  const effectiveEarthRadiusKm = 6371.0 * 4.0 / 3.0;
  const theta = elevationDeg * Math.PI / 180.0;
  const slantKm = horizontalRangeKm / Math.max(Math.cos(theta), 0.02);
  return Math.sqrt(
    slantKm * slantKm +
    effectiveEarthRadiusKm * effectiveEarthRadiusKm +
    2.0 * slantKm * effectiveEarthRadiusKm * Math.sin(theta)
  ) - effectiveEarthRadiusKm;
}

function kmToKft(km) {
  return km * 3.280839895;
}

function updateTiltHud() {
  if (state.selectedElevation == null) return;

  el("tiltDegree").textContent =
    Number(state.selectedElevation).toFixed(2) + "°";
  el("tiltPosition").textContent = state.anchorScan
    ? `${state.anchorScan.label} · ${scanAgeLabel(state.anchorScan)} · ${scanTimestamp(state.anchorScan)}`
    : "No scan available";

  const activeRange = state.hoverRangeKm ?? state.point?.range_km ?? null;
  if (activeRange != null) {
    const hKm = beamHeightArlKm(activeRange, state.selectedElevation);
    el("beamHeightHud").textContent =
      `Beam center ~${kmToKft(hKm).toFixed(1)} kft ARL @ ${Number(activeRange).toFixed(1)} km`;
    el("currentHeightValue").textContent =
      kmToKft(hKm).toFixed(1) + " kft";
  } else {
    el("beamHeightHud").textContent =
      "Move cursor over radar for beam height";
  }

  el("cursorHud").textContent =
    state.hoverRangeKm != null
      ? `Az ${state.hoverAzimuth.toFixed(1)}° · Range ${state.hoverRangeKm.toFixed(1)} km`
      : "Drag pan · wheel zoom";

  el("currentTiltValue").textContent =
    Number(state.selectedElevation).toFixed(2) + "°";

  const index = selectedSlotIndex();
  el("tiltDownBtn").disabled = index <= 0;
  el("tiltUpBtn").disabled =
    index < 0 || index >= state.slots.length - 1;
}

function viewTransform() {
  return `translate3d(${state.view.x * 100}%, ${state.view.y * 100}%, 0) scale(${state.view.scale})`;
}

function applyTransform(node) {
  if (!node) return;
  node.style.transformOrigin = "50% 50%";
  node.style.transform = viewTransform();
}

function applyView() {
  applyTransform(radarImage);
  applyTransform(el("boundaryOverlay"));
  applyTransform(el("overlay"));

  for (const tile of state.wallTiles) {
    applyTransform(tile.img);
    applyTransform(tile.boundary);
  }

  el("resetViewBtn").disabled =
    Math.abs(state.view.scale - 1) < 0.001 &&
    Math.abs(state.view.x) < 0.001 &&
    Math.abs(state.view.y) < 0.001;
}

function resetView() {
  state.view = {scale: 1, x: 0, y: 0};
  applyView();
}

function zoomBy(delta) {
  const factor = delta < 0 ? 1.18 : 1 / 1.18;
  state.view.scale = Math.max(
    1,
    Math.min(8, state.view.scale * factor)
  );

  if (state.view.scale <= 1.001) {
    state.view.scale = 1;
    state.view.x = 0;
    state.view.y = 0;
  }

  applyView();
}

function startPan(event, surface, tileIndex = null) {
  if (event.button !== 0) return;
  surface.setPointerCapture?.(event.pointerId);
  state.pointer = {
    id: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    baseX: state.view.x,
    baseY: state.view.y,
    width: Math.max(1, surface.clientWidth),
    height: Math.max(1, surface.clientHeight),
    moved: false,
    surface,
    tileIndex,
  };
  surface.classList.add("panning");
}

function movePan(event) {
  const p = state.pointer;
  if (!p || event.pointerId !== p.id) return;

  const dx = event.clientX - p.startX;
  const dy = event.clientY - p.startY;
  if (Math.hypot(dx, dy) > 4) p.moved = true;

  state.view.x = p.baseX + dx / p.width;
  state.view.y = p.baseY + dy / p.height;

  const maxPan = Math.max(0, (state.view.scale - 1) / (2 * state.view.scale)) + 0.15;
  state.view.x = Math.max(-maxPan, Math.min(maxPan, state.view.x));
  state.view.y = Math.max(-maxPan, Math.min(maxPan, state.view.y));
  applyView();
}

async function endPan(event) {
  const p = state.pointer;
  if (!p || event.pointerId !== p.id) return;

  p.surface.classList.remove("panning");
  p.surface.releasePointerCapture?.(event.pointerId);
  state.pointer = null;

  if (!p.moved) {
    if (p.tileIndex != null) {
      await selectWallTile(p.tileIndex);
    } else {
      inspectMainAtEvent(event);
    }
  }
}

function attachSurfaceNavigation(surface, tileIndex = null) {
  surface.addEventListener("pointerdown", (event) =>
    startPan(event, surface, tileIndex)
  );
  surface.addEventListener("pointermove", movePan);
  surface.addEventListener("pointerup", endPan);
  surface.addEventListener("pointercancel", endPan);
  surface.addEventListener("wheel", (event) => {
    event.preventDefault();
    if (event.shiftKey && !state.wallMode) {
      stepElevation(event.deltaY < 0 ? 1 : -1);
    } else {
      zoomBy(event.deltaY);
    }
  }, {passive: false});
  surface.addEventListener("dblclick", (event) => {
    event.preventDefault();
    resetView();
  });
}

function renderSingle() {
  updateTimeUI();
  updateTiltHud();

  const scan = state.anchorScan;
  if (!scan) {
    radarImage.removeAttribute("src");
    el("productLabel").textContent = "No scan available";
    return;
  }

  const key = imageKey(scan, 640);
  if (radarImage.dataset.key !== key) {
    radarImage.dataset.key = key;
    radarImage.src = scanUrl(scan, 640);
  }

  const fieldMeta = FIELD_OPTIONS.find((item) => item[0] === state.field);
  el("productLabel").textContent =
    (fieldMeta?.[1] || state.field) +
    (fieldMeta?.[2] ? " · " + fieldMeta[2] : "") +
    (state.smooth ? " · 2-D interpolated" : "");
  el("sweepLabel").textContent =
    `${Number(state.selectedElevation).toFixed(2)}° · ${scan.label} · ${scanAgeLabel(scan)}`;

  const prev = state.scanHistory[state.scanIndex + 1];
  const next = state.scanHistory[state.scanIndex - 1];
  [prev, next].filter(Boolean).forEach((item) => {
    const preload = new Image();
    preload.src = scanUrl(item, 640);
  });

  renderBoundaries();
  applyView();
}

function ensureWallTiles() {
  if (state.wallTiles.length === 16) return;

  const wall = el("tiltWall");
  wall.innerHTML = "";
  state.wallTiles = [];

  for (let i = 0; i < 16; i++) {
    const root = document.createElement("div");
    root.className = "wall-tile";
    root.dataset.index = String(i);

    const img = document.createElement("img");
    img.alt = "";
    img.draggable = false;

    const boundary = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "svg"
    );
    boundary.setAttribute("class", "wall-boundary");
    boundary.setAttribute("aria-hidden", "true");

    const label = document.createElement("div");
    label.className = "wall-label";

    const age = document.createElement("div");
    age.className = "wall-progress";

    const tag = document.createElement("div");
    tag.className = "wall-tag hidden";

    root.append(img, boundary, label, age, tag);
    wall.appendChild(root);

    attachSurfaceNavigation(root, i);
    state.wallTiles.push({root, img, boundary, label, age, tag});
  }
}

function renderWall() {
  ensureWallTiles();

  const physicalNow = state.scanStatus?.current || null;
  const expectedNext = state.scanStatus?.expected_next || null;

  for (let i = 0; i < 16; i++) {
    const tile = state.wallTiles[i];
    const slot = state.wallSlots[i] || null;

    tile.root.classList.remove(
      "active", "supp-current", "next-scan", "wall-empty"
    );
    tile.tag.classList.add("hidden");

    if (!slot) {
      tile.root.classList.add("wall-empty");
      tile.label.textContent = "—";
      tile.age.textContent = "NO TILT";
      tile.img.removeAttribute("src");
      tile.img.dataset.key = "";
      continue;
    }

    tile.label.textContent = Number(slot.elevation).toFixed(2) + "°";
    const scan = slot.scan;

    if (sameElevation(slot.elevation, state.selectedElevation)) {
      tile.root.classList.add("active");
    }

    if (physicalNow && sameElevation(physicalNow.elevation, slot.elevation) &&
        physicalNow.kind !== "BASE") {
      tile.root.classList.add("supp-current");
      tile.tag.classList.remove("hidden");
      tile.tag.textContent = "NOW · " + physicalNow.label;
    } else if (expectedNext &&
               sameElevation(expectedNext.elevation, slot.elevation)) {
      tile.root.classList.add("next-scan");
      tile.tag.classList.remove("hidden");
      tile.tag.textContent = "NEXT · " + expectedNext.label;
    }

    if (!scan) {
      tile.root.classList.add("wall-empty");
      tile.age.textContent = "NO SCAN";
      tile.img.removeAttribute("src");
      tile.img.dataset.key = "";
      continue;
    }

    tile.age.textContent =
      scanAgeLabel(scan) + " · " + scanTimestamp(scan);

    const key = imageKey(scan, 220);
    if (tile.img.dataset.key !== key) {
      tile.img.dataset.key = key;
      tile.img.src = scanUrl(scan, 220);
      tile.img.alt =
        `${Number(slot.elevation).toFixed(2)} degree ${state.field}`;
    }
  }

  const anchor = state.anchorScan;
  el("wallStatus").textContent = anchor
    ? `ANCHOR ${Number(state.selectedElevation).toFixed(2)}° · ${scanTimestamp(anchor)} · ${scanAgeLabel(anchor)}`
    : "No anchor scan";

  renderBoundaries();
  applyView();
}

function renderAll() {
  renderSingle();
  if (state.wallMode) renderWall();
}

function segmentsToPath(segments) {
  let out = "";
  for (const segment of segments || []) {
    if (!segment.length) continue;
    out += "M" + segment.map((p) => p[0] + "," + (-p[1])).join("L");
  }
  return out;
}

function boundarySvgMarkup() {
  if (!state.boundaries) return "";
  return (
    `<path class="county" d="${segmentsToPath(state.boundaries.counties)}"></path>` +
    `<path class="state" d="${segmentsToPath(state.boundaries.states)}"></path>`
  );
}

function renderBoundaries() {
  if (!state.boundaries) return;
  const r = state.rangeKm;
  const markup = boundarySvgMarkup();

  const main = el("boundaryOverlay");
  main.setAttribute("viewBox", `${-r} ${-r} ${2 * r} ${2 * r}`);
  if (main.dataset.loaded !== "1") {
    main.innerHTML = markup;
    main.dataset.loaded = "1";
  }

  for (const tile of state.wallTiles) {
    tile.boundary.setAttribute(
      "viewBox",
      `${-r} ${-r} ${2 * r} ${2 * r}`
    );
    if (tile.boundary.dataset.loaded !== "1") {
      tile.boundary.innerHTML = markup;
      tile.boundary.dataset.loaded = "1";
    }
  }
}

async function loadBoundaries() {
  if (state.boundaries || state.boundariesLoading) return;
  state.boundariesLoading = true;
  try {
    state.boundaries = await json("/api/boundaries?range_km=330");
    renderBoundaries();
    applyView();
  } catch (err) {
    console.warn("Boundary load failed", err);
  } finally {
    state.boundariesLoading = false;
  }
}

async function selectWallTile(index) {
  const slot = state.wallSlots[index];
  if (!slot?.scan) {
    toast("No scan is available for that elevation.");
    return;
  }

  state.selectedElevation = Number(slot.elevation);
  const selectorIndex = state.slots.findIndex((item) =>
    sameElevation(item.elevation, slot.elevation)
  );
  if (selectorIndex >= 0) sweepSelect.value = String(selectorIndex);

  state.followLatest = false;
  await loadScanHistory(slot.scan.id, false);
  state.anchorScan = state.scanHistory[state.scanIndex] || slot.scan;
  await resolveWall(state.anchorScan);
}

async function stepTime(direction) {
  if (!state.scanHistory.length) return;

  const nextIndex = state.scanIndex + direction;
  if (nextIndex < 0) {
    toast("Already on the newest scan for this elevation.");
    return;
  }
  if (nextIndex >= state.scanHistory.length) {
    toast("Oldest cached scan reached.");
    return;
  }

  state.followLatest = false;
  state.scanIndex = nextIndex;
  state.anchorScan = state.scanHistory[state.scanIndex];
  updateTimeUI();
  await resolveWall(state.anchorScan);
}

async function goLatest() {
  state.followLatest = true;
  await loadScanHistory(null, true);
  state.anchorScan = state.scanHistory[0] || null;
  await resolveWall(state.anchorScan);
}

async function stepElevation(direction) {
  const current = selectedSlotIndex();
  const next = current + direction;
  if (next < 0 || next >= state.slots.length) return;

  const slot = state.slots[next];
  state.selectedElevation = Number(slot.elevation);
  sweepSelect.value = String(next);

  // Keep the same temporal context: select the scan currently displayed in
  // the wall at this elevation, not an arbitrary newest scan.
  const wallSlot = state.wallSlots.find((item) =>
    sameElevation(item.elevation, slot.elevation)
  );
  const preferred = wallSlot?.scan?.id || null;
  state.followLatest = false;
  await loadScanHistory(preferred, false);
  if (preferred) {
    const found = state.scanHistory.findIndex((scan) => scan.id === preferred);
    if (found >= 0) state.scanIndex = found;
  }
  state.anchorScan = state.scanHistory[state.scanIndex] || wallSlot?.scan || null;
  await resolveWall(state.anchorScan);
}

function setWallMode(enabled) {
  state.wallMode = Boolean(enabled);
  el("singleView").classList.toggle("hidden", state.wallMode);
  el("wallView").classList.toggle("hidden", !state.wallMode);
  el("wallToggleBtn").textContent =
    state.wallMode ? "Single Panel" : "16 Panel";

  if (state.wallMode) {
    ensureWallTiles();
    renderWall();
  } else {
    renderSingle();
  }
}

function eventCoordinates(event) {
  const rect = radarStage.getBoundingClientRect();
  const screenX = (event.clientX - rect.left) / rect.width;
  const screenY = (event.clientY - rect.top) / rect.height;

  // Undo the shared pan/zoom transform before converting to radar coordinates.
  const localX = ((screenX - 0.5 - state.view.x) / state.view.scale) + 0.5;
  const localY = ((screenY - 0.5 - state.view.y) / state.view.scale) + 0.5;

  const xKm = (localX * 2 - 1) * state.rangeKm;
  const yKm = (1 - localY * 2) * state.rangeKm;
  const rangeKm = Math.hypot(xKm, yKm);
  const azimuth =
    (Math.atan2(xKm, yKm) * 180 / Math.PI + 360) % 360;

  return {xKm, yKm, rangeKm, azimuth, localX, localY};
}

async function inspectMainAtEvent(event) {
  if (state.wallMode) return;
  const p = eventCoordinates(event);

  cursorMarker.classList.remove("hidden");
  cursorMarker.setAttribute(
    "transform",
    `translate(${p.localX * 1000 - 500} ${p.localY * 1000 - 500})`
  );

  state.point = {
    x_km: p.xKm,
    y_km: p.yKm,
    range_km: p.rangeKm,
    azimuth: p.azimuth,
  };
  el("pointBadge").textContent =
    `${p.azimuth.toFixed(1)}° / ${p.rangeKm.toFixed(1)} km`;
  el("azValue").textContent = p.azimuth.toFixed(1) + "°";
  el("rangeValue").textContent = p.rangeKm.toFixed(1) + " km";
  el("analysisContent").classList.remove("hidden");
  el("emptyState").classList.add("hidden");
  updateTiltHud();
}

radarStage.addEventListener("mousemove", (event) => {
  if (state.pointer) return;
  const p = eventCoordinates(event);
  state.hoverRangeKm = p.rangeKm;
  state.hoverAzimuth = p.azimuth;

  if (state.selectedElevation != null) {
    const hKm = beamHeightArlKm(p.rangeKm, state.selectedElevation);
    el("cursorReadout").textContent =
      `Az ${p.azimuth.toFixed(1)}° · ${p.rangeKm.toFixed(1)} km · beam ~${kmToKft(hKm).toFixed(1)} kft ARL`;
  }
  updateTiltHud();
});

radarStage.addEventListener("mouseleave", () => {
  if (state.pointer) return;
  state.hoverRangeKm = null;
  state.hoverAzimuth = null;
  el("cursorReadout").textContent =
    "Drag to pan · wheel to zoom · double-click to reset";
  updateTiltHud();
});

document.addEventListener("keydown", (event) => {
  const tag = event.target?.tagName?.toLowerCase();
  if (["input", "select", "textarea"].includes(tag)) return;

  if (event.key === "ArrowUp") {
    event.preventDefault();
    stepElevation(1);
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    stepElevation(-1);
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    stepTime(1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    stepTime(-1);
  }
});

el("tiltUpBtn").addEventListener("click", () => stepElevation(1));
el("tiltDownBtn").addEventListener("click", () => stepElevation(-1));
el("timePastBtn").addEventListener("click", () => stepTime(1));
el("timeFutureBtn").addEventListener("click", () => stepTime(-1));
el("goLiveBtn").addEventListener("click", goLatest);
el("wallToggleBtn").addEventListener("click", () =>
  setWallMode(!state.wallMode)
);
el("resetViewBtn").addEventListener("click", resetView);

fieldSelect.addEventListener("change", () => {
  state.field = fieldSelect.value;
  renderAll();
});

sweepSelect.addEventListener("change", async () => {
  const index = Number(sweepSelect.value);
  const slot = state.slots[index];
  if (!slot) return;

  state.selectedElevation = Number(slot.elevation);
  const wallSlot = state.wallSlots.find((item) =>
    sameElevation(item.elevation, slot.elevation)
  );
  const preferred = wallSlot?.scan?.id || null;
  state.followLatest = false;
  await loadScanHistory(preferred, false);
  state.anchorScan = state.scanHistory[state.scanIndex] || wallSlot?.scan || null;
  await resolveWall(state.anchorScan);
});

smoothToggle.addEventListener("change", () => {
  state.smooth = smoothToggle.checked;
  renderAll();
});

rangeSelect.addEventListener("change", () => {
  state.rangeKm = Number(rangeSelect.value);
  cursorMarker.classList.add("hidden");
  resetView();

  el("boundaryOverlay").dataset.loaded = "";
  for (const tile of state.wallTiles) {
    tile.boundary.dataset.loaded = "";
  }
  renderAll();
});

el("refreshBtn").addEventListener("click", async () => {
  const button = el("refreshBtn");
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const result = await json("/api/refresh", {method: "POST"});
    updateStatus(result);
    if (state.followLatest) {
      await loadElevations({preserveAnchor: false});
    } else {
      await loadElevations({preserveAnchor: true});
    }
    toast(result.changed ? "Radar scans updated." : "Already current.");
  } catch (err) {
    toast("Radar refresh failed.");
  } finally {
    button.disabled = false;
    button.textContent = "Refresh";
  }
});

(async function init() {
  populateFields();
  state.smooth = smoothToggle.checked;
  attachSurfaceNavigation(radarStage, null);
  ensureWallTiles();

  await loadStatus();
  await loadElevations({preserveAnchor: false});
  loadBoundaries();
  applyView();

  setInterval(loadStatus, 2000);
})();

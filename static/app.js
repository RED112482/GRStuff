const state = {
  slots: [],
  selectedElevation: null,
  field: "reflectivity",
  rangeKm: 150,
  smooth: true,
  scanHistory: [],
  scanIndex: 0,
  wallMode: false,
  point: null,
  hoverRangeKm: null,
  hoverAzimuth: null,
  wheelLocked: false,
  boundaries: null,
  boundariesLoading: false,
  lastStatus: null,
  scanSequence: [],
  scanStatus: {},
  lastLiveToken: null,
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
  const t = el("toast");
  t.textContent = message;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 2600);
}

async function json(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || response.statusText);
  }
  return response.json();
}

function fmtUtc(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().slice(11, 19) + "Z";
}

function currentScan() {
  return state.scanHistory[state.scanIndex] || null;
}

function selectedSlot() {
  if (state.selectedElevation === null) return null;
  return state.slots.find(
    (slot) => Math.abs(Number(slot.elevation) - Number(state.selectedElevation)) <= 0.06
  ) || null;
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

function fillElevationSelect() {
  const prior = state.selectedElevation;
  sweepSelect.innerHTML = "";

  state.slots.forEach((slot, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    const scan = slot.latest;
    let suffix = "";
    if (scan) {
      if (scan.source === "live") {
        suffix = scan.kind === "BASE" ? " · LIVE" : ` · ${scan.kind} #${scan.sequence_number}`;
      } else {
        suffix = ` · last available -${scan.volume_offset} vol`;
      }
    } else {
      suffix = " · unavailable";
    }
    option.textContent = Number(slot.elevation).toFixed(2) + "°" + suffix;
    sweepSelect.appendChild(option);
  });

  if (!state.slots.length) {
    state.selectedElevation = null;
    return;
  }

  let selectedIndex = 0;
  if (prior !== null) {
    let best = Infinity;
    state.slots.forEach((slot, index) => {
      const delta = Math.abs(Number(slot.elevation) - Number(prior));
      if (delta < best) {
        best = delta;
        selectedIndex = index;
      }
    });
  }

  state.selectedElevation = Number(state.slots[selectedIndex].elevation);
  sweepSelect.value = String(selectedIndex);
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

function scanAgeLabel(scan) {
  if (!scan) return "NO SCAN";
  if (scan.source === "live") {
    if (scan.kind === "BASE") return "LIVE";
    return `${scan.kind} #${scan.sequence_number}`;
  }
  if (scan.volume_offset === 1) return "PREV VOL";
  return `-${scan.volume_offset} VOL`;
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
    el("statusText").textContent = "Waiting for live chunk feed";
    el("volumeTime").textContent = "Latest complete " + fmtUtc(data.archive_volume_time);
  } else {
    setLiveBadge("offline", "OFFLINE");
    el("statusText").textContent = "Waiting for radar data";
    el("volumeTime").textContent = data.live_error || data.error || "No data";
  }
}

async function loadStatus() {
  try {
    const data = await json("/api/status");
    const tokenChanged = data.live_token !== state.lastLiveToken;
    state.lastLiveToken = data.live_token;
    updateStatus(data);

    if (tokenChanged) {
      const preserveId = currentScan()?.id || null;
      await loadElevations(preserveId);
    }
  } catch (err) {
    setLiveBadge("offline", "OFFLINE");
    el("statusText").textContent = "Backend unavailable";
    el("volumeTime").textContent = err.message;
  }
}

function renderScanSequence() {
  const container = el("scanSequence");
  container.innerHTML = "";

  for (let i = 0; i < state.scanSequence.length; i++) {
    const scan = state.scanSequence[i];
    const chip = document.createElement("div");
    chip.className = "scan-chip " + String(scan.kind || "BASE").toLowerCase();

    if (i < state.scanSequence.length - 1 || Number(scan.completion ?? 100) >= 99.5) {
      chip.classList.add("complete");
    }
    if (i === state.scanSequence.length - 1 && state.lastStatus?.live_available) {
      chip.classList.add("current");
    }

    chip.textContent = scan.label || Number(scan.elevation).toFixed(2) + "°";
    container.appendChild(chip);
  }

  if (state.scanStatus?.expected_next) {
    const next = state.scanStatus.expected_next;
    const chip = document.createElement("div");
    chip.className = "scan-chip expected " + String(next.kind || "BASE").toLowerCase();
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
  const next = state.scanStatus?.expected_next;
  el("scanNowLabel").textContent = now
    ? "NOW: " + now.label + (Number(now.completion ?? 100) < 99.5 ? ` · ${Math.round(now.completion)}%` : "")
    : "Waiting for scan metadata…";
  el("scanNextLabel").textContent = next ? "NEXT: " + next.label : "";

  requestAnimationFrame(() => {
    container.scrollLeft = container.scrollWidth;
  });
}

async function loadElevations(preserveScanId = null) {
  try {
    const data = await json("/api/elevations");
    const priorElevation = state.selectedElevation;

    state.slots = data.slots || [];
    state.scanSequence = data.scan_sequence || [];
    state.scanStatus = data.scan_status || {};

    fillElevationSelect();
    renderScanSequence();

    if (priorElevation !== null && state.slots.length) {
      let bestIndex = 0;
      let best = Infinity;
      state.slots.forEach((slot, index) => {
        const delta = Math.abs(Number(slot.elevation) - Number(priorElevation));
        if (delta < best) {
          best = delta;
          bestIndex = index;
        }
      });
      state.selectedElevation = Number(state.slots[bestIndex].elevation);
      sweepSelect.value = String(bestIndex);
    }

    await loadScanHistory(preserveScanId);

    if (state.wallMode) renderWall();
  } catch (err) {
    console.error("Elevation load failed", err);
    toast("Could not load elevation inventory.");
  }
}

async function loadScanHistory(preserveScanId = null, resetToNewest = false) {
  if (state.selectedElevation === null) {
    state.scanHistory = [];
    state.scanIndex = 0;
    updateTimeUI();
    return;
  }

  try {
    const data = await json(
      "/api/scan-history?elevation=" +
      encodeURIComponent(Number(state.selectedElevation).toFixed(2)) +
      "&limit=10"
    );

    const oldIndex = state.scanIndex;
    state.scanHistory = data.scans || [];

    if (resetToNewest) {
      state.scanIndex = 0;
    } else if (preserveScanId) {
      const found = state.scanHistory.findIndex((scan) => scan.id === preserveScanId);
      state.scanIndex = found >= 0 ? found : Math.min(oldIndex, Math.max(0, state.scanHistory.length - 1));
    } else {
      state.scanIndex = Math.min(oldIndex, Math.max(0, state.scanHistory.length - 1));
    }

    updateTimeUI();
    renderSingleRadar();
  } catch (err) {
    console.error("Scan history failed", err);
    state.scanHistory = [];
    state.scanIndex = 0;
    updateTimeUI();
  }
}

function updateTimeUI() {
  const scan = currentScan();
  el("timePastBtn").disabled = !scan || state.scanIndex >= state.scanHistory.length - 1;
  el("timeFutureBtn").disabled = !scan || state.scanIndex <= 0;
  el("goLiveBtn").classList.toggle("active", state.scanIndex === 0);

  if (!scan) {
    el("timeLabel").textContent = "NO SCAN";
    el("timeStamp").textContent = "—";
    return;
  }

  el("timeLabel").textContent =
    state.scanIndex === 0 ? scanAgeLabel(scan) : `-${state.scanIndex} SCAN`;
  el("timeStamp").textContent =
    fmtUtc(scan.scan_time || scan.volume_time);
}

async function stepTime(direction) {
  if (!state.scanHistory.length) {
    toast("No scans are available for this elevation.");
    return;
  }

  const next = state.scanIndex + direction;
  if (next < 0) {
    toast("Already on the newest available scan.");
    return;
  }
  if (next >= state.scanHistory.length) {
    toast("Oldest cached scan reached.");
    return;
  }

  state.scanIndex = next;
  updateTimeUI();
  renderSingleRadar();
}

function selectedSlotIndex() {
  return state.slots.findIndex(
    (slot) => Math.abs(Number(slot.elevation) - Number(state.selectedElevation)) <= 0.06
  );
}

async function stepElevation(direction) {
  const index = selectedSlotIndex();
  if (index < 0) return;

  const next = state.slots[index + direction];
  if (!next) return;

  state.selectedElevation = Number(next.elevation);
  sweepSelect.value = String(index + direction);
  state.scanIndex = 0;
  await loadScanHistory(null, true);
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
  const scan = currentScan();
  const elevation = state.selectedElevation;

  if (elevation === null) {
    el("tiltDegree").textContent = "—°";
    el("tiltPosition").textContent = "No tilt";
    return;
  }

  el("tiltDegree").textContent = Number(elevation).toFixed(2) + "°";
  el("tiltPosition").textContent = scan
    ? `${scan.label} · ${scanAgeLabel(scan)}`
    : "No scan available";

  const activeRange = state.hoverRangeKm ?? state.point?.range_km ?? null;
  if (activeRange !== null) {
    const hKm = beamHeightArlKm(Number(activeRange), Number(elevation));
    el("beamHeightHud").textContent =
      `Beam center ~${kmToKft(hKm).toFixed(1)} kft ARL @ ${Number(activeRange).toFixed(1)} km`;
    el("currentHeightValue").textContent = kmToKft(hKm).toFixed(1) + " kft";
  } else {
    el("beamHeightHud").textContent = "Move cursor over radar for beam height";
  }

  if (state.hoverRangeKm !== null && state.hoverAzimuth !== null) {
    el("cursorHud").textContent =
      `Az ${state.hoverAzimuth.toFixed(1)}° · Range ${state.hoverRangeKm.toFixed(1)} km`;
  } else {
    el("cursorHud").textContent = "Wheel ↑ higher tilt · Wheel ↓ lower tilt";
  }

  el("currentTiltValue").textContent = Number(elevation).toFixed(2) + "°";

  const index = selectedSlotIndex();
  el("tiltDownBtn").disabled = index <= 0;
  el("tiltUpBtn").disabled = index < 0 || index >= state.slots.length - 1;
}

function renderSingleRadar() {
  const scan = currentScan();
  updateTiltHud();
  updateTimeUI();

  if (!scan) {
    radarImage.removeAttribute("src");
    el("productLabel").textContent = "No scan available";
    el("sweepLabel").textContent =
      state.selectedElevation === null ? "—" : Number(state.selectedElevation).toFixed(2) + "°";
    return;
  }

  const url = scanUrl(scan, 640);
  radarImage.src = url;

  radarImage.onerror = () => {
    el("productLabel").textContent =
      "Moment unavailable in this scan · use ← for previous available scan";
  };

  const fieldMeta = FIELD_OPTIONS.find((item) => item[0] === state.field);
  el("productLabel").textContent =
    (fieldMeta?.[1] || state.field) +
    (fieldMeta?.[2] ? " · " + fieldMeta[2] : "") +
    (state.smooth ? " · 2-D interpolated" : "");
  el("sweepLabel").textContent =
    `${Number(state.selectedElevation).toFixed(2)}° · ${scan.label} · ${scanAgeLabel(scan)}`;

  const older = state.scanHistory[state.scanIndex + 1];
  const newer = state.scanHistory[state.scanIndex - 1];
  [older, newer].filter(Boolean).forEach((item) => {
    const preload = new Image();
    preload.src = scanUrl(item, 640);
  });

  renderBoundaries();
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

  const svg = el("boundaryOverlay");
  svg.setAttribute("viewBox", `${-r} ${-r} ${2 * r} ${2 * r}`);
  svg.innerHTML = boundarySvgMarkup();

  document.querySelectorAll(".wall-boundary").forEach((wallSvg) => {
    wallSvg.setAttribute("viewBox", `${-r} ${-r} ${2 * r} ${2 * r}`);
    wallSvg.innerHTML = boundarySvgMarkup();
  });
}

async function loadBoundaries() {
  if (state.boundaries || state.boundariesLoading) {
    renderBoundaries();
    return;
  }

  state.boundariesLoading = true;
  try {
    state.boundaries = await json("/api/boundaries?range_km=330");
    renderBoundaries();
  } catch (err) {
    console.warn("Boundary load failed", err);
  } finally {
    state.boundariesLoading = false;
  }
}

function sameElevation(a, b) {
  return a !== null && a !== undefined &&
    b !== null && b !== undefined &&
    Math.abs(Number(a) - Number(b)) <= 0.06;
}

function renderWall() {
  const wall = el("tiltWall");
  wall.innerHTML = "";

  const currentPhysical = state.scanStatus?.current || null;
  const expectedNext = state.scanStatus?.expected_next || null;

  for (let i = 0; i < 16; i++) {
    const slot = state.slots[i];
    const tile = document.createElement("div");
    tile.className = "wall-tile";

    if (!slot) {
      tile.classList.add("wall-empty");
      tile.textContent = "No base tilt";
      wall.appendChild(tile);
      continue;
    }

    const scan = slot.latest;
    const currentMatch = currentPhysical && sameElevation(currentPhysical.elevation, slot.elevation);
    const nextMatch = expectedNext && sameElevation(expectedNext.elevation, slot.elevation);

    if (sameElevation(slot.elevation, state.selectedElevation)) {
      tile.classList.add("active");
    }
    if (currentMatch && currentPhysical.kind !== "BASE") {
      tile.classList.add("supp-current");
    } else if (nextMatch) {
      tile.classList.add("next-scan");
    }

    if (scan) {
      const img = document.createElement("img");
      img.alt = Number(slot.elevation).toFixed(2) + " degree " + state.field;
      img.src = scanUrl(scan, 300);
      img.onerror = () => {
        img.style.opacity = "0.14";
      };
      tile.appendChild(img);
    } else {
      const waiting = document.createElement("div");
      waiting.className = "wall-empty";
      waiting.style.position = "absolute";
      waiting.style.inset = "0";
      waiting.textContent = "NO SCAN";
      tile.appendChild(waiting);
    }

    const bsvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    bsvg.setAttribute("class", "wall-boundary");
    bsvg.setAttribute("aria-hidden", "true");

    const label = document.createElement("div");
    label.className = "wall-label";
    label.textContent = Number(slot.elevation).toFixed(2) + "°";

    const progress = document.createElement("div");
    progress.className = "wall-progress";
    progress.textContent = scan ? scanAgeLabel(scan) : "WAIT";

    tile.append(bsvg, label, progress);

    if (currentMatch && currentPhysical.kind !== "BASE") {
      const tag = document.createElement("div");
      tag.className = "wall-tag";
      tag.textContent = "NOW · " + currentPhysical.label;
      tile.appendChild(tag);
    } else if (nextMatch && expectedNext) {
      const tag = document.createElement("div");
      tag.className = "wall-tag";
      tag.textContent = "NEXT · " + expectedNext.label;
      tile.appendChild(tag);
    }

    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute(
      "aria-label",
      "Open " + Number(slot.elevation).toFixed(2) + " degree tilt"
    );
    button.addEventListener("click", async () => {
      state.selectedElevation = Number(slot.elevation);
      sweepSelect.value = String(i);
      state.scanIndex = 0;
      setWallMode(false);
      await loadScanHistory(null, true);
    });
    tile.appendChild(button);

    wall.appendChild(tile);
  }

  const nowText = currentPhysical ? "NOW " + currentPhysical.label : "No live physical cut yet";
  const nextText = expectedNext ? " · NEXT " + expectedNext.label : "";
  el("wallStatus").textContent = nowText + nextText;

  renderBoundaries();
}

function setWallMode(enabled) {
  state.wallMode = Boolean(enabled);
  el("singleView").classList.toggle("hidden", state.wallMode);
  el("wallView").classList.toggle("hidden", !state.wallMode);
  el("wallToggleBtn").textContent = state.wallMode ? "Single Panel" : "16 Panel";

  if (state.wallMode) {
    renderWall();
  } else {
    renderSingleRadar();
  }
}

async function selectElevationByIndex(index) {
  const slot = state.slots[index];
  if (!slot) return;

  state.selectedElevation = Number(slot.elevation);
  state.scanIndex = 0;
  sweepSelect.value = String(index);
  await loadScanHistory(null, true);
}

function renderInspector(data) {
  state.point = data;
  el("emptyState").classList.add("hidden");
  el("analysisContent").classList.remove("hidden");
  el("pointBadge").textContent =
    `${data.azimuth.toFixed(1)}° / ${data.range_km.toFixed(1)} km`;
  el("azValue").textContent = data.azimuth.toFixed(1) + "°";
  el("rangeValue").textContent = data.range_km.toFixed(1) + " km";

  const tbody = el("inspectorRows");
  tbody.innerHTML = "";

  for (const row of data.rows || []) {
    const tr = document.createElement("tr");
    const values = [
      Number(row.elevation).toFixed(2) + "°",
      kmToKft(Number(row.height_km)).toFixed(1) + " kft",
      row.values?.reflectivity == null ? "—" : Number(row.values.reflectivity).toFixed(1),
      row.values?.velocity == null ? "—" : Number(row.values.velocity).toFixed(1),
      row.values?.differential_reflectivity == null ? "—" : Number(row.values.differential_reflectivity).toFixed(2),
      row.values?.cross_correlation_ratio == null ? "—" : Number(row.values.cross_correlation_ratio).toFixed(3),
    ];
    values.forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
}

async function inspectAt(xKm, yKm) {
  try {
    const data = await json(
      `/api/inspect?x_km=${Number(xKm).toFixed(3)}&y_km=${Number(yKm).toFixed(3)}&frame=0`
    );
    renderInspector(data);
  } catch (err) {
    console.error(err);
  }
}

function eventCoordinates(event) {
  const rect = radarStage.getBoundingClientRect();
  const px = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  const py = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height));
  const xKm = (px * 2 - 1) * state.rangeKm;
  const yKm = (1 - py * 2) * state.rangeKm;
  const rangeKm = Math.hypot(xKm, yKm);
  const azimuth = (Math.atan2(xKm, yKm) * 180 / Math.PI + 360) % 360;
  return {px, py, xKm, yKm, rangeKm, azimuth};
}

radarStage.addEventListener("mousemove", (event) => {
  const p = eventCoordinates(event);
  state.hoverRangeKm = p.rangeKm;
  state.hoverAzimuth = p.azimuth;

  if (state.selectedElevation !== null) {
    const hKm = beamHeightArlKm(p.rangeKm, state.selectedElevation);
    el("cursorReadout").textContent =
      `Az ${p.azimuth.toFixed(1)}° · ${p.rangeKm.toFixed(1)} km · beam ~${kmToKft(hKm).toFixed(1)} kft ARL`;
  }
  updateTiltHud();
});

radarStage.addEventListener("mouseleave", () => {
  state.hoverRangeKm = null;
  state.hoverAzimuth = null;
  el("cursorReadout").textContent =
    state.point
      ? `Selected: az ${state.point.azimuth.toFixed(1)}° · range ${state.point.range_km.toFixed(1)} km`
      : "Move over radar for azimuth, range and beam height";
  updateTiltHud();
});

radarStage.addEventListener("click", (event) => {
  const p = eventCoordinates(event);
  cursorMarker.classList.remove("hidden");
  cursorMarker.setAttribute(
    "transform",
    `translate(${p.px * 1000 - 500} ${p.py * 1000 - 500})`
  );
  radarStage.focus({preventScroll: true});
  inspectAt(p.xKm, p.yKm);
});

radarStage.addEventListener("wheel", (event) => {
  event.preventDefault();
  if (state.wheelLocked) return;
  state.wheelLocked = true;
  stepElevation(event.deltaY < 0 ? 1 : -1);
  setTimeout(() => { state.wheelLocked = false; }, 60);
}, {passive: false});

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
el("goLiveBtn").addEventListener("click", () => {
  state.scanIndex = 0;
  updateTimeUI();
  renderSingleRadar();
});
el("wallToggleBtn").addEventListener("click", () => setWallMode(!state.wallMode));

fieldSelect.addEventListener("change", () => {
  state.field = fieldSelect.value;
  if (state.wallMode) renderWall();
  else renderSingleRadar();
});

sweepSelect.addEventListener("change", () => {
  selectElevationByIndex(Number(sweepSelect.value));
});

smoothToggle.addEventListener("change", () => {
  state.smooth = smoothToggle.checked;
  if (state.wallMode) renderWall();
  else renderSingleRadar();
});

rangeSelect.addEventListener("change", () => {
  state.rangeKm = Number(rangeSelect.value);
  cursorMarker.classList.add("hidden");
  state.point = null;
  renderBoundaries();
  if (state.wallMode) renderWall();
  else renderSingleRadar();
});

el("refreshBtn").addEventListener("click", async () => {
  const button = el("refreshBtn");
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const result = await json("/api/refresh", {method: "POST"});
    updateStatus(result);
    await loadElevations(currentScan()?.id || null);
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

  await loadStatus();
  await loadElevations();
  loadBoundaries();

  setInterval(loadStatus, 2000);
})();

const state = {
  volume: null,
  field: "reflectivity",
  sweep: 0,
  rangeKm: 150,
  smooth: true,
  point: null,
  hoverRangeKm: null,
  hoverAzimuth: null,
  renderToken: 0,
  lastStatus: null,
  wheelLocked: false,
  frame: 0,
  history: [],
  maxFrame: 0,
  wallMode: false,
  boundaries: null,
  boundariesLoading: false,
};

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
  return d.toISOString().slice(11, 19) + "Z";
}

function humanAge(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) {
    return "age unknown";
  }
  const sec = Math.max(0, Math.round(Number(seconds)));
  if (sec < 60) return sec + "s old";
  const min = Math.floor(sec / 60);
  const rem = sec % 60;
  return min + "m " + rem + "s old";
}

function setLiveBadge(mode, text) {
  const badge = el("liveBadge");
  badge.classList.remove("live", "history", "offline");
  if (mode) badge.classList.add(mode);
  el("liveText").textContent = text;
}

function currentFrameMeta() {
  return state.history.find((item) => item.frame === state.frame) || null;
}

function updateTimeUI() {
  const meta = currentFrameMeta();
  el("timePastBtn").disabled = state.frame >= state.maxFrame;
  el("timeFutureBtn").disabled = state.frame <= 0;
  el("goLiveBtn").classList.toggle("active", state.frame === 0);

  if (state.frame === 0) {
    el("timeLabel").textContent = state.volume?.source === "live" ? "LIVE" : "LATEST";
  } else {
    el("timeLabel").textContent = "-" + state.frame + " VOL";
  }

  el("timeStamp").textContent = fmtUtc(state.volume?.volume_time || meta?.volume_time);
}

function updateStatus(data) {
  state.lastStatus = data;
  const dot = el("statusDot");
  dot.classList.toggle("ok", !!data.loaded);
  dot.classList.toggle("bad", !data.loaded || (!!data.error && !data.live_available));

  if (state.frame > 0) {
    setLiveBadge("history", "HISTORY");
    el("statusText").textContent = "Historical volume";
    el("volumeTime").textContent =
      "Frame -" + state.frame + " · " + fmtUtc(state.volume?.volume_time);
    updateTimeUI();
    return;
  }

  if (data.live_available) {
    setLiveBadge("live", data.live_complete ? "LIVE · COMPLETE" : "LIVE · SCANNING");
    el("statusText").textContent = "AWS Level-II chunk stream";
    el("volumeTime").textContent =
      "Volume " + fmtUtc(data.live_volume_time) +
      " · " + humanAge(data.live_age_seconds) +
      " · " + data.live_chunk_count + " chunks";
  } else if (data.archive_loaded) {
    setLiveBadge(data.xradar_available ? "history" : "offline", "ARCHIVE");
    el("statusText").textContent = data.xradar_available
      ? "Waiting for real-time chunk stream"
      : "Xradar 0.12+ needed for true live mode";
    el("volumeTime").textContent =
      "Latest complete " + fmtUtc(data.archive_volume_time) +
      " · " + humanAge(data.archive_age_seconds);
  } else {
    setLiveBadge("offline", "OFFLINE");
    el("statusText").textContent = "Waiting for radar data";
    el("volumeTime").textContent = data.live_error || data.error || "No volume loaded";
  }

  updateTimeUI();
}

async function loadHistory() {
  try {
    const data = await json("/api/history");
    state.history = data.frames || [];
    state.maxFrame = Number(data.max_frame || 0);
    if (state.frame > state.maxFrame) state.frame = state.maxFrame;
    updateTimeUI();
  } catch (err) {
    console.error("History load failed", err);
  }
}

async function loadStatus() {
  try {
    const data = await json("/api/status");
    const previousToken = state.lastStatus?.live_token;
    const previousArchive = state.lastStatus?.archive_key;
    updateStatus(data);

    if (state.frame === 0) {
      const liveChanged = data.live_available && data.live_token && data.live_token !== state.volume?.key;
      const archiveChanged = !data.live_available && data.archive_key && data.archive_key !== state.volume?.key;
      if (liveChanged || archiveChanged) {
        await loadHistory();
        await loadVolume(true);
      }
    } else if (data.archive_key && data.archive_key !== previousArchive) {
      await loadHistory();
    }

    if (data.live_token !== previousToken && state.wallMode && state.frame === 0) {
      renderWall();
    }
  } catch (err) {
    setLiveBadge("offline", "OFFLINE");
    el("statusText").textContent = "Backend unavailable";
    el("volumeTime").textContent = err.message;
    el("statusDot").classList.add("bad");
  }
}

function getSweep() {
  return state.volume?.sweeps.find((s) => s.index === state.sweep) || null;
}

function getSweepPosition() {
  if (!state.volume) return -1;
  return state.volume.sweeps.findIndex((s) => s.index === state.sweep);
}

function fillControls(volume, priorElevation = null) {
  const currentField = state.field;
  fieldSelect.innerHTML = "";

  volume.fields.forEach((f) => {
    const o = document.createElement("option");
    o.value = f.id;
    o.textContent = f.label;
    fieldSelect.appendChild(o);
  });

  state.field = volume.fields.some((f) => f.id === currentField)
    ? currentField
    : (volume.fields[0]?.id || "reflectivity");
  fieldSelect.value = state.field;

  sweepSelect.innerHTML = "";
  volume.sweeps.forEach((s) => {
    const o = document.createElement("option");
    o.value = s.index;
    const progress = Number(s.completion ?? 100);
    o.textContent = progress < 99.5
      ? s.elevation.toFixed(2) + "° · " + Math.round(progress) + "%"
      : s.elevation.toFixed(2) + "°";
    sweepSelect.appendChild(o);
  });

  if (priorElevation !== null && volume.sweeps.length) {
    let nearest = volume.sweeps[0];
    let distance = Math.abs(nearest.elevation - priorElevation);
    for (const s of volume.sweeps) {
      const d = Math.abs(s.elevation - priorElevation);
      if (d < distance) {
        nearest = s;
        distance = d;
      }
    }
    state.sweep = nearest.index;
  } else if (!volume.sweeps.some((s) => s.index === state.sweep)) {
    state.sweep = volume.sweeps[0]?.index || 0;
  }

  sweepSelect.value = String(state.sweep);
}

function imageUrl(sweep = state.sweep, size = 640) {
  if (!state.volume) return "";
  const params = new URLSearchParams({
    range_km: String(state.rangeKm),
    smooth: state.smooth ? "true" : "false",
    frame: String(state.frame),
    size: String(size),
    v: state.volume.key || "unknown",
  });
  return `/api/image/${encodeURIComponent(state.field)}/${sweep}.png?${params.toString()}`;
}

function prefetchSweep(sweep) {
  if (!state.volume?.sweeps.some((s) => s.index === sweep)) return;
  const image = new Image();
  image.src = imageUrl(sweep, 640);
}

function prefetchNeighbors() {
  const pos = getSweepPosition();
  if (pos < 0 || state.wallMode) return;
  for (const offset of [-1, 1]) {
    const neighbor = state.volume.sweeps[pos + offset];
    if (neighbor) prefetchSweep(neighbor.index);
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

function exactSelectedHeightKm() {
  if (!state.point) return null;
  const row = state.point.rows?.find((r) => r.sweep === state.sweep);
  return row ? Number(row.height_km) : null;
}

function updateTiltHud() {
  const sweep = getSweep();
  if (!sweep || !state.volume) return;

  const pos = getSweepPosition();
  const completion = Number(sweep.completion ?? 100);
  el("tiltDegree").textContent = sweep.elevation.toFixed(2) + "°";
  el("tiltPosition").textContent =
    `Tilt ${pos + 1} of ${state.volume.sweeps.length}` +
    (completion < 99.5 ? ` · ${Math.round(completion)}% received` : "");
  el("sweepLabel").textContent =
    `${sweep.elevation.toFixed(2)}° elevation · tilt ${pos + 1}/${state.volume.sweeps.length}`;
  el("currentTiltValue").textContent = sweep.elevation.toFixed(2) + "°";

  const exactKm = exactSelectedHeightKm();
  const activeRange = state.hoverRangeKm ?? state.point?.range_km ?? null;

  if (exactKm !== null && state.hoverRangeKm === null) {
    const kft = kmToKft(exactKm);
    el("beamHeightHud").textContent = `Beam center ${kft.toFixed(1)} kft ARL at selected point`;
    el("currentHeightValue").textContent = kft.toFixed(1) + " kft";
  } else if (activeRange !== null) {
    const approxKm = beamHeightArlKm(Number(activeRange), sweep.elevation);
    el("beamHeightHud").textContent =
      `Beam center ~${kmToKft(approxKm).toFixed(1)} kft ARL @ ${Number(activeRange).toFixed(1)} km`;
    if (exactKm === null) el("currentHeightValue").textContent = "—";
  } else {
    el("beamHeightHud").textContent = "Move cursor over radar for beam height";
    el("currentHeightValue").textContent =
      exactKm === null ? "—" : kmToKft(exactKm).toFixed(1) + " kft";
  }

  if (state.hoverRangeKm !== null && state.hoverAzimuth !== null) {
    el("cursorHud").textContent =
      `Az ${state.hoverAzimuth.toFixed(1)}° · Range ${state.hoverRangeKm.toFixed(1)} km`;
  } else {
    el("cursorHud").textContent = "Wheel ↑ higher tilt · Wheel ↓ lower tilt";
  }

  el("tiltDownBtn").disabled = pos <= 0;
  el("tiltUpBtn").disabled = pos >= state.volume.sweeps.length - 1;
  updateInspectorHighlight();
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
  const countyPath = segmentsToPath(state.boundaries.counties);
  const statePath = segmentsToPath(state.boundaries.states);
  return `<path class="county" d="${countyPath}"></path><path class="state" d="${statePath}"></path>`;
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
    toast("State/county outlines could not be loaded.");
  } finally {
    state.boundariesLoading = false;
  }
}

function renderSingleRadar() {
  if (!state.volume || !state.field) return;

  const url = imageUrl(state.sweep, 640);
  const token = ++state.renderToken;
  const loader = new Image();

  loader.onload = () => {
    if (token !== state.renderToken) return;
    radarImage.src = url;
    updateTiltHud();
    renderBoundaries();
    setTimeout(prefetchNeighbors, 0);
  };

  loader.onerror = () => {
    if (token === state.renderToken) {
      toast("This moment is not available on that tilt yet.");
    }
  };

  loader.src = url;

  const field = state.volume.fields.find((f) => f.id === state.field);
  el("productLabel").textContent = field
    ? `${field.label}${field.units ? " · " + field.units : ""}${state.smooth ? " · 2-D interpolated" : ""}`
    : state.field;
  updateTiltHud();
}

function renderWall() {
  if (!state.volume || !state.field) return;

  const wall = el("tiltWall");
  wall.innerHTML = "";

  const tilts = state.volume.sweeps.slice(0, 16);
  for (let i = 0; i < 16; i++) {
    const tilt = tilts[i];
    const tile = document.createElement("div");
    tile.className = "wall-tile";

    if (!tilt) {
      tile.classList.add("wall-empty");
      tile.textContent = "No additional base tilt";
      wall.appendChild(tile);
      continue;
    }

    if (tilt.index === state.sweep) tile.classList.add("active");

    const img = document.createElement("img");
    img.alt = `${tilt.elevation.toFixed(2)} degree ${state.field}`;
    img.src = imageUrl(tilt.index, 300);

    const label = document.createElement("div");
    label.className = "wall-label";
    label.textContent = tilt.elevation.toFixed(2) + "°";

    const progress = document.createElement("div");
    const completion = Number(tilt.completion ?? 100);
    progress.className = "wall-progress" + (completion < 99.5 ? " scanning" : "");
    progress.textContent = completion < 99.5
      ? Math.max(1, Math.round(completion)) + "%"
      : "READY";

    const bsvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    bsvg.setAttribute("class", "wall-boundary");
    bsvg.setAttribute("aria-hidden", "true");

    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-label", "Open " + tilt.elevation.toFixed(2) + " degree tilt");
    button.addEventListener("click", () => {
      state.sweep = tilt.index;
      sweepSelect.value = String(tilt.index);
      setWallMode(false);
      renderSingleRadar();
    });

    img.addEventListener("error", () => {
      img.style.opacity = "0.18";
      progress.textContent = "WAIT";
      progress.classList.add("scanning");
    });

    tile.append(img, bsvg, label, progress, button);
    wall.appendChild(tile);
  }

  const incomplete = state.volume.sweeps.filter((s) => Number(s.completion ?? 100) < 99.5);
  if (state.frame === 0 && state.volume.source === "live") {
    el("wallStatus").textContent = incomplete.length
      ? `${state.volume.sweeps.length} base tilts · receiving ${incomplete[incomplete.length - 1].elevation.toFixed(2)}° now`
      : `${state.volume.sweeps.length} base tilts · live volume complete`;
  } else {
    el("wallStatus").textContent =
      `${state.volume.sweeps.length} base tilts · ${fmtUtc(state.volume.volume_time)}`;
  }

  renderBoundaries();
}

function renderRadar() {
  if (state.wallMode) renderWall();
  else renderSingleRadar();
}

function setWallMode(enabled) {
  state.wallMode = !!enabled;
  el("singleView").classList.toggle("hidden", state.wallMode);
  el("wallView").classList.toggle("hidden", !state.wallMode);
  el("wallToggleBtn").textContent = state.wallMode ? "Single Panel" : "16 Panel";
  if (state.wallMode) renderWall();
  else renderSingleRadar();
}

function setSweep(sweepIndex) {
  if (!state.volume?.sweeps.some((s) => s.index === sweepIndex)) return;
  state.sweep = sweepIndex;
  sweepSelect.value = String(sweepIndex);
  renderRadar();
}

function stepSweep(direction) {
  const pos = getSweepPosition();
  if (pos < 0) return;
  const next = state.volume.sweeps[pos + direction];
  if (next) setSweep(next.index);
}

async function setFrame(frame) {
  const nextFrame = Math.max(0, Math.min(state.maxFrame, Number(frame)));
  if (nextFrame === state.frame && state.volume) return;

  const priorElevation = getSweep()?.elevation ?? null;
  const selectedX = state.point?.x_km;
  const selectedY = state.point?.y_km;

  state.frame = nextFrame;
  state.point = null;
  el("analysisContent").classList.add("hidden");
  el("emptyState").classList.remove("hidden");
  updateTimeUI();

  await loadVolume(false, priorElevation);

  if (selectedX !== undefined && selectedY !== undefined) {
    await inspectAt(selectedX, selectedY, false);
  }
}

async function loadVolume(force = false, priorElevationOverride = null) {
  try {
    const previousSweep = getSweep();
    const priorElevation = priorElevationOverride ?? previousSweep?.elevation ?? null;
    const selectedX = state.point?.x_km;
    const selectedY = state.point?.y_km;
    const oldKey = state.volume?.key;

    const volume = await json("/api/volume?frame=" + state.frame);
    state.volume = volume;
    fillControls(volume, priorElevation);
    renderRadar();
    updateTimeUI();

    if (force || oldKey !== volume.key) {
      if (selectedX !== undefined && selectedY !== undefined) {
        await inspectAt(selectedX, selectedY, false);
      }
    }

    loadBoundaries();
    if (state.lastStatus) updateStatus(state.lastStatus);
  } catch (err) {
    toast("Could not load radar volume.");
    console.error(err);
  }
}

function value(row, field, digits = 1) {
  const v = row.values?.[field];
  return v === null || v === undefined ? "—" : Number(v).toFixed(digits);
}

function updateInspectorHighlight() {
  const rows = el("inspectorRows").querySelectorAll("tr[data-sweep]");
  rows.forEach((row) => {
    row.classList.toggle("current", Number(row.dataset.sweep) === state.sweep);
  });

  if (state.point) {
    const active = state.point.rows?.find((r) => r.sweep === state.sweep);
    if (active) {
      el("currentHeightValue").textContent =
        kmToKft(Number(active.height_km)).toFixed(1) + " kft";
    }
  }
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

  data.rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.dataset.sweep = String(row.sweep);
    tr.title = "Jump to " + row.elevation.toFixed(2) + "°";
    tr.addEventListener("click", () => setSweep(row.sweep));

    const vals = [
      row.elevation.toFixed(2) + "°",
      kmToKft(Number(row.height_km)).toFixed(1) + " kft",
      value(row, "reflectivity"),
      value(row, "velocity"),
      value(row, "differential_reflectivity", 2),
      value(row, "cross_correlation_ratio", 3),
    ];

    vals.forEach((v) => {
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    });

    tbody.appendChild(tr);
  });

  updateTiltHud();
  updateInspectorHighlight();
}

async function inspectAt(xKm, yKm, showErrors = true) {
  try {
    const data = await json(
      `/api/inspect?x_km=${Number(xKm).toFixed(3)}&y_km=${Number(yKm).toFixed(3)}&frame=${state.frame}`
    );
    renderInspector(data);
  } catch (err) {
    if (showErrors) toast("Column analysis failed at this point.");
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
  const sweep = getSweep();
  if (sweep) {
    const hKm = beamHeightArlKm(p.rangeKm, sweep.elevation);
    el("cursorReadout").textContent =
      `Az ${p.azimuth.toFixed(1)}° · ${p.rangeKm.toFixed(1)} km · beam ~${kmToKft(hKm).toFixed(1)} kft ARL`;
  }
  updateTiltHud();
});

radarStage.addEventListener("mouseleave", () => {
  state.hoverRangeKm = null;
  state.hoverAzimuth = null;
  if (state.point) {
    el("cursorReadout").textContent =
      `Selected: az ${state.point.azimuth.toFixed(1)}° · range ${state.point.range_km.toFixed(1)} km`;
  } else {
    el("cursorReadout").textContent =
      "Move over radar for azimuth, range and beam height";
  }
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
  stepSweep(event.deltaY < 0 ? 1 : -1);
  setTimeout(() => { state.wheelLocked = false; }, 55);
}, {passive: false});

document.addEventListener("keydown", (event) => {
  const tag = event.target?.tagName?.toLowerCase();
  if (["input", "select", "textarea"].includes(tag)) return;

  if (event.key === "ArrowUp") {
    event.preventDefault();
    stepSweep(1);
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    stepSweep(-1);
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    setFrame(state.frame + 1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    setFrame(state.frame - 1);
  }
});

el("tiltUpBtn").addEventListener("click", () => stepSweep(1));
el("tiltDownBtn").addEventListener("click", () => stepSweep(-1));
el("timePastBtn").addEventListener("click", () => setFrame(state.frame + 1));
el("timeFutureBtn").addEventListener("click", () => setFrame(state.frame - 1));
el("goLiveBtn").addEventListener("click", () => setFrame(0));
el("wallToggleBtn").addEventListener("click", () => setWallMode(!state.wallMode));

fieldSelect.addEventListener("change", () => {
  state.field = fieldSelect.value;
  renderRadar();
});

sweepSelect.addEventListener("change", () => {
  setSweep(Number(sweepSelect.value));
});

smoothToggle.addEventListener("change", () => {
  state.smooth = smoothToggle.checked;
  renderRadar();
});

rangeSelect.addEventListener("change", () => {
  state.rangeKm = Number(rangeSelect.value);
  cursorMarker.classList.add("hidden");
  state.point = null;
  state.hoverRangeKm = null;
  state.hoverAzimuth = null;
  el("emptyState").classList.remove("hidden");
  el("analysisContent").classList.add("hidden");
  el("pointBadge").textContent = "No point selected";
  el("cursorReadout").textContent =
    "Move over radar for azimuth, range and beam height";
  renderBoundaries();
  renderRadar();
});

el("refreshBtn").addEventListener("click", async () => {
  const button = el("refreshBtn");
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const result = await json("/api/refresh", {method: "POST"});
    updateStatus(result);
    await loadHistory();
    await loadVolume(true);
    toast(result.changed ? "Radar data updated." : "Already current.");
  } catch (err) {
    toast("Radar refresh failed.");
  } finally {
    button.disabled = false;
    button.textContent = "Refresh";
  }
});

(async function init() {
  state.smooth = smoothToggle.checked;
  await loadHistory();
  await loadStatus();
  if (!state.volume) await loadVolume();
  loadBoundaries();
  setInterval(loadStatus, 2000);
})();

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
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return "age unknown";
  const sec = Math.max(0, Math.round(Number(seconds)));
  if (sec < 60) return sec + "s old";
  const min = Math.floor(sec / 60);
  const rem = sec % 60;
  return min + "m " + rem + "s old";
}

function currentAgeSeconds() {
  const iso = state.lastStatus?.volume_time || state.volume?.volume_time;
  if (!iso) return null;
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

function updateStatus(data) {
  state.lastStatus = data;
  const dot = el("statusDot");
  dot.classList.toggle("ok", !!data.loaded);
  dot.classList.toggle("bad", !data.loaded && !!data.error);
  el("statusText").textContent = data.loaded ? "Rapid AWS volume monitor active" : "Waiting for Level-II data";
  if (data.error) {
    el("volumeTime").textContent = data.error;
  } else if (data.loaded) {
    el("volumeTime").textContent =
      "Volume " + fmtUtc(data.volume_time) + " · " + humanAge(data.age_seconds) +
      " · checks every " + data.poll_seconds + "s";
  } else {
    el("volumeTime").textContent = "Waiting for volume metadata";
  }
}

function refreshAgeLabel() {
  if (!state.lastStatus?.loaded || state.lastStatus?.error) return;
  el("volumeTime").textContent =
    "Volume " + fmtUtc(state.lastStatus.volume_time) + " · " + humanAge(currentAgeSeconds()) +
    " · checks every " + state.lastStatus.poll_seconds + "s";
}

async function loadStatus() {
  try {
    const data = await json("/api/status");
    updateStatus(data);
    if (data.loaded && (!state.volume || data.key !== state.volume.key)) {
      await loadVolume();
    }
  } catch (err) {
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
    : volume.fields[0]?.id;
  fieldSelect.value = state.field;

  const elevationCounts = new Map();
  volume.sweeps.forEach((s) => {
    const key = s.elevation.toFixed(2);
    elevationCounts.set(key, (elevationCounts.get(key) || 0) + 1);
  });
  const seen = new Map();

  sweepSelect.innerHTML = "";
  volume.sweeps.forEach((s) => {
    const key = s.elevation.toFixed(2);
    const cut = (seen.get(key) || 0) + 1;
    seen.set(key, cut);
    const o = document.createElement("option");
    o.value = s.index;
    o.textContent = elevationCounts.get(key) > 1
      ? `${s.elevation.toFixed(2)}° · cut ${cut}`
      : `${s.elevation.toFixed(2)}°`;
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

function imageUrl(sweep = state.sweep) {
  if (!state.volume) return "";
  const params = new URLSearchParams({
    range_km: String(state.rangeKm),
    smooth: state.smooth ? "true" : "false",
    v: state.volume.key || "unknown",
  });
  return `/api/image/${encodeURIComponent(state.field)}/${sweep}.png?${params.toString()}`;
}

function prefetchSweep(sweep) {
  if (!state.volume?.sweeps.some((s) => s.index === sweep)) return;
  const image = new Image();
  image.src = imageUrl(sweep);
}

function prefetchNeighbors() {
  const pos = getSweepPosition();
  if (pos < 0) return;
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
  el("tiltDegree").textContent = sweep.elevation.toFixed(2) + "°";
  el("tiltPosition").textContent = `Tilt ${pos + 1} of ${state.volume.sweeps.length} · sweep ${sweep.index}`;
  el("sweepLabel").textContent = `${sweep.elevation.toFixed(2)}° elevation · tilt ${pos + 1}/${state.volume.sweeps.length}`;
  el("currentTiltValue").textContent = sweep.elevation.toFixed(2) + "°";

  const exactKm = exactSelectedHeightKm();
  const activeRange = state.hoverRangeKm ?? state.point?.range_km ?? null;

  if (exactKm !== null && state.hoverRangeKm === null) {
    const kft = kmToKft(exactKm);
    el("beamHeightHud").textContent = `Beam center ${kft.toFixed(1)} kft ARL at selected point`;
    el("currentHeightValue").textContent = kft.toFixed(1) + " kft";
  } else if (activeRange !== null) {
    const approxKm = beamHeightArlKm(Number(activeRange), sweep.elevation);
    el("beamHeightHud").textContent = `Beam center ~${kmToKft(approxKm).toFixed(1)} kft ARL @ ${Number(activeRange).toFixed(1)} km`;
    if (exactKm === null) el("currentHeightValue").textContent = "—";
  } else {
    el("beamHeightHud").textContent = "Move cursor over radar for beam height";
    el("currentHeightValue").textContent = exactKm === null ? "—" : kmToKft(exactKm).toFixed(1) + " kft";
  }

  if (state.hoverRangeKm !== null && state.hoverAzimuth !== null) {
    el("cursorHud").textContent = `Az ${state.hoverAzimuth.toFixed(1)}° · Range ${state.hoverRangeKm.toFixed(1)} km`;
  } else {
    el("cursorHud").textContent = "Wheel ↑ higher tilt · Wheel ↓ lower tilt";
  }

  el("tiltDownBtn").disabled = pos <= 0;
  el("tiltUpBtn").disabled = pos >= state.volume.sweeps.length - 1;
  updateInspectorHighlight();
}

function renderRadar() {
  if (!state.volume || !state.field) return;

  const url = imageUrl();
  const token = ++state.renderToken;
  const loader = new Image();

  loader.onload = () => {
    if (token !== state.renderToken) return;
    radarImage.src = url;
    updateTiltHud();
    setTimeout(prefetchNeighbors, 0);
  };

  loader.onerror = () => {
    if (token === state.renderToken) toast("Radar image rendering failed.");
  };

  loader.src = url;

  const field = state.volume.fields.find((f) => f.id === state.field);
  el("productLabel").textContent = field
    ? `${field.label}${field.units ? " · " + field.units : ""}${state.smooth ? " · 2-D interpolated" : ""}`
    : state.field;
  updateTiltHud();
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

async function loadVolume() {
  try {
    const previousSweep = getSweep();
    const priorElevation = previousSweep?.elevation ?? null;
    const selectedX = state.point?.x_km;
    const selectedY = state.point?.y_km;
    const oldKey = state.volume?.key;

    const volume = await json("/api/volume");
    state.volume = volume;
    fillControls(volume, priorElevation);
    renderRadar();

    updateStatus({
      ...(state.lastStatus || {}),
      loaded: true,
      key: volume.key,
      volume_time: volume.volume_time,
      loaded_at: volume.loaded_at,
      age_seconds: volume.age_seconds,
      poll_seconds: state.lastStatus?.poll_seconds ?? 5,
      error: null,
    });

    if (oldKey && oldKey !== volume.key) {
      toast("New KMOB volume loaded.");
      if (selectedX !== undefined && selectedY !== undefined) {
        await inspectAt(selectedX, selectedY, false);
      }
    }
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
      el("currentHeightValue").textContent = kmToKft(Number(active.height_km)).toFixed(1) + " kft";
    }
  }
}

function renderInspector(data) {
  state.point = data;
  el("emptyState").classList.add("hidden");
  el("analysisContent").classList.remove("hidden");
  el("pointBadge").textContent = `${data.azimuth.toFixed(1)}° / ${data.range_km.toFixed(1)} km`;
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
    const data = await json(`/api/inspect?x_km=${Number(xKm).toFixed(3)}&y_km=${Number(yKm).toFixed(3)}`);
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
    el("cursorReadout").textContent = "Move over radar for azimuth, range and beam height";
  }
  updateTiltHud();
});

radarStage.addEventListener("click", (event) => {
  const p = eventCoordinates(event);
  cursorMarker.classList.remove("hidden");
  cursorMarker.setAttribute("transform", `translate(${p.px * 1000 - 500} ${p.py * 1000 - 500})`);
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
  }
});

el("tiltUpBtn").addEventListener("click", () => stepSweep(1));
el("tiltDownBtn").addEventListener("click", () => stepSweep(-1));

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
  el("cursorReadout").textContent = "Move over radar for azimuth, range and beam height";
  renderRadar();
});

el("refreshBtn").addEventListener("click", async () => {
  const button = el("refreshBtn");
  button.disabled = true;
  button.textContent = "Checking AWS…";
  try {
    const result = await json("/api/refresh", {method: "POST"});
    updateStatus(result);
    if (result.changed || !state.volume) await loadVolume();
    toast(result.changed ? "New KMOB volume loaded." : "Already on newest completed volume.");
  } catch (err) {
    toast("AWS refresh failed.");
  } finally {
    button.disabled = false;
    button.textContent = "Refresh now";
  }
});

(async function init() {
  state.smooth = smoothToggle.checked;
  await loadStatus();
  if (!state.volume) await loadVolume();
  setInterval(loadStatus, 5000);
  setInterval(refreshAgeLabel, 1000);
})();

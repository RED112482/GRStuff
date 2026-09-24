const state = {
  volume: null,
  field: "reflectivity",
  sweep: 0,
  rangeKm: 150,
  point: null,
};

const el = (id) => document.getElementById(id);
const fieldSelect = el("fieldSelect");
const sweepSelect = el("sweepSelect");
const rangeSelect = el("rangeSelect");
const radarImage = el("radarImage");
const radarStage = el("radarStage");
const cursorMarker = el("cursorMarker");

function toast(message) {
  const t = el("toast");
  t.textContent = message;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 3500);
}

async function json(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || response.statusText);
  }
  return response.json();
}

function fmtTime(iso) {
  if (!iso) return "No volume loaded";
  return new Date(iso).toLocaleString([], {hour12:false});
}

function updateStatus(data) {
  const dot = el("statusDot");
  dot.classList.toggle("ok", !!data.loaded);
  dot.classList.toggle("bad", !data.loaded && !!data.error);
  el("statusText").textContent = data.loaded ? "Live AWS ingest active" : "Waiting for Level-II data";
  el("volumeTime").textContent = data.error || fmtTime(data.loaded_at);
}

async function loadStatus() {
  try {
    updateStatus(await json("/api/status"));
  } catch (err) {
    el("statusText").textContent = "Backend unavailable";
    el("volumeTime").textContent = err.message;
    el("statusDot").classList.add("bad");
  }
}

function fillControls(volume) {
  const currentField = state.field;
  fieldSelect.innerHTML = "";
  volume.fields.forEach((f) => {
    const o = document.createElement("option");
    o.value = f.id;
    o.textContent = f.label;
    fieldSelect.appendChild(o);
  });
  state.field = volume.fields.some(f => f.id === currentField) ? currentField : volume.fields[0]?.id;
  fieldSelect.value = state.field;

  const priorSweep = state.sweep;
  sweepSelect.innerHTML = "";
  volume.sweeps.forEach((s) => {
    const o = document.createElement("option");
    o.value = s.index;
    o.textContent = `${s.elevation.toFixed(2)}°  ·  sweep ${s.index}`;
    sweepSelect.appendChild(o);
  });
  state.sweep = volume.sweeps.some(s => s.index === priorSweep) ? priorSweep : 0;
  sweepSelect.value = String(state.sweep);
}

function renderRadar() {
  if (!state.volume || !state.field) return;
  const stamp = Date.now();
  radarImage.src = `/api/image/${encodeURIComponent(state.field)}/${state.sweep}.png?range_km=${state.rangeKm}&t=${stamp}`;
  const field = state.volume.fields.find(f => f.id === state.field);
  const sweep = state.volume.sweeps.find(s => s.index === state.sweep);
  el("productLabel").textContent = field ? `${field.label}${field.units ? " · " + field.units : ""}` : state.field;
  el("sweepLabel").textContent = sweep ? `${sweep.elevation.toFixed(2)}° elevation` : `Sweep ${state.sweep}`;
}

async function loadVolume() {
  try {
    const volume = await json("/api/volume");
    const oldKey = state.volume?.key;
    state.volume = volume;
    fillControls(volume);
    renderRadar();
    updateStatus({loaded:true, loaded_at:volume.loaded_at, error:null});
    if (oldKey && oldKey !== volume.key) toast("New KMOB volume loaded.");
  } catch (err) {
    toast("Could not load radar volume.");
    console.error(err);
  }
}

function value(row, field, digits=1) {
  const v = row.values?.[field];
  return v === null || v === undefined ? "—" : Number(v).toFixed(digits);
}

function renderInspector(data) {
  state.point = data;
  el("emptyState").classList.add("hidden");
  el("analysisContent").classList.remove("hidden");
  el("pointBadge").textContent = `${data.azimuth.toFixed(1)}° / ${data.range_km.toFixed(1)} km`;
  el("azValue").textContent = `${data.azimuth.toFixed(1)}°`;
  el("rangeValue").textContent = `${data.range_km.toFixed(1)} km`;
  el("tiltCount").textContent = String(data.rows.length);
  el("cursorReadout").textContent = `Selected: az ${data.azimuth.toFixed(1)}° · range ${data.range_km.toFixed(1)} km`;

  const tbody = el("inspectorRows");
  tbody.innerHTML = "";
  data.rows.forEach((row) => {
    const tr = document.createElement("tr");
    const vals = [
      `${row.elevation.toFixed(2)}°`,
      `${row.height_km.toFixed(2)} km`,
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
}

async function inspectAt(xKm, yKm) {
  try {
    const data = await json(`/api/inspect?x_km=${xKm.toFixed(3)}&y_km=${yKm.toFixed(3)}`);
    renderInspector(data);
  } catch (err) {
    toast("Column analysis failed at this point.");
    console.error(err);
  }
}

radarStage.addEventListener("click", (event) => {
  const rect = radarStage.getBoundingClientRect();
  const px = (event.clientX - rect.left) / rect.width;
  const py = (event.clientY - rect.top) / rect.height;
  const xKm = (px * 2 - 1) * state.rangeKm;
  const yKm = (1 - py * 2) * state.rangeKm;

  cursorMarker.classList.remove("hidden");
  const sx = px * 1000;
  const sy = py * 1000;
  cursorMarker.setAttribute("transform", `translate(${sx - 500} ${sy - 500})`);
  inspectAt(xKm, yKm);
});

fieldSelect.addEventListener("change", () => {
  state.field = fieldSelect.value;
  renderRadar();
});

sweepSelect.addEventListener("change", () => {
  state.sweep = Number(sweepSelect.value);
  renderRadar();
});

rangeSelect.addEventListener("change", () => {
  state.rangeKm = Number(rangeSelect.value);
  cursorMarker.classList.add("hidden");
  state.point = null;
  el("emptyState").classList.remove("hidden");
  el("analysisContent").classList.add("hidden");
  el("pointBadge").textContent = "No point selected";
  el("cursorReadout").textContent = "Click the radar to inspect the column";
  renderRadar();
});

el("refreshBtn").addEventListener("click", async () => {
  const button = el("refreshBtn");
  button.disabled = true;
  button.textContent = "Checking AWS…";
  try {
    const result = await json("/api/refresh", {method:"POST"});
    updateStatus(result);
    await loadVolume();
    toast(result.changed ? "New KMOB volume loaded." : "Already on the newest KMOB volume.");
  } catch (err) {
    toast("AWS refresh failed.");
  } finally {
    button.disabled = false;
    button.textContent = "Refresh now";
  }
});

radarImage.addEventListener("error", () => toast("Radar image rendering failed."));

(async function init() {
  await loadStatus();
  await loadVolume();
  setInterval(loadStatus, 15000);
  setInterval(loadVolume, 30000);
})();

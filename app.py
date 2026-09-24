from __future__ import annotations

import asyncio
import io
import math
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pyart
from botocore import UNSIGNED
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

RADAR_ID = os.getenv("RADAR_ID", "KMOB").upper()
BUCKET = os.getenv("NEXRAD_BUCKET", "unidata-nexrad-level2")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
DEFAULT_RANGE_KM = float(os.getenv("DEFAULT_RANGE_KM", "150"))
CACHE_DIR = Path(os.getenv("RADAR_CACHE_DIR", Path(tempfile.gettempdir()) / "kmob-level2"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

FIELD_CONFIG = {
    "reflectivity": {"label": "Reflectivity", "units": "dBZ", "vmin": -10, "vmax": 75, "cmap": "turbo"},
    "velocity": {"label": "Velocity", "units": "m/s", "vmin": -40, "vmax": 40, "cmap": "seismic"},
    "differential_reflectivity": {"label": "ZDR", "units": "dB", "vmin": -2, "vmax": 8, "cmap": "Spectral_r"},
    "cross_correlation_ratio": {"label": "CC", "units": "", "vmin": 0.70, "vmax": 1.0, "cmap": "viridis"},
    "differential_phase": {"label": "PhiDP", "units": "deg", "vmin": 0, "vmax": 180, "cmap": "twilight"},
    "spectrum_width": {"label": "Spectrum Width", "units": "m/s", "vmin": 0, "vmax": 15, "cmap": "magma"},
}

@dataclass
class VolumeState:
    radar: Any | None = None
    key: str | None = None
    loaded_at: datetime | None = None
    file_path: Path | None = None
    error: str | None = None

state = VolumeState()
state_lock = threading.RLock()
s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))
app = FastAPI(title="KMOB Level-II Volume Explorer", version="0.1.0")

def _candidate_prefixes(now: datetime) -> list[str]:
    return [f"{d:%Y/%m/%d}/{RADAR_ID}/" for d in (now, now - timedelta(days=1))]

def find_latest_key() -> str:
    candidates: list[tuple[datetime, str]] = []
    for prefix in _candidate_prefixes(datetime.now(timezone.utc)):
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                name = key.rsplit("/", 1)[-1]
                if "_MDM" in name:
                    continue
                if not (name.startswith(RADAR_ID) and ("_V06" in name or name.endswith(".gz"))):
                    continue
                candidates.append((obj["LastModified"], key))
    if not candidates:
        raise RuntimeError(f"No recent Level-II files found for {RADAR_ID}.")
    return max(candidates, key=lambda item: item[0])[1]

def load_key(key: str) -> None:
    local_path = CACHE_DIR / key.rsplit("/", 1)[-1]
    if not local_path.exists():
        s3.download_file(BUCKET, key, str(local_path))
    radar = pyart.io.read_nexrad_archive(str(local_path), station=RADAR_ID)
    with state_lock:
        old_path = state.file_path
        state.radar = radar
        state.key = key
        state.loaded_at = datetime.now(timezone.utc)
        state.file_path = local_path
        state.error = None
    if old_path and old_path != local_path and old_path.exists():
        try:
            old_path.unlink()
        except OSError:
            pass

def refresh_volume() -> bool:
    try:
        key = find_latest_key()
        with state_lock:
            if key == state.key and state.radar is not None:
                return False
        load_key(key)
        return True
    except Exception as exc:
        with state_lock:
            state.error = f"{type(exc).__name__}: {exc}"
        return False

async def poll_loop() -> None:
    while True:
        await asyncio.to_thread(refresh_volume)
        await asyncio.sleep(POLL_SECONDS)

@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(poll_loop())

def get_radar():
    with state_lock:
        radar = state.radar
    if radar is None:
        refresh_volume()
        with state_lock:
            radar = state.radar
    if radar is None:
        raise HTTPException(status_code=503, detail=state.error or "Radar volume is not loaded yet.")
    return radar

@app.get("/api/status")
def api_status() -> dict[str, Any]:
    with state_lock:
        return {
            "radar": RADAR_ID,
            "bucket": BUCKET,
            "loaded": state.radar is not None,
            "key": state.key,
            "loaded_at": state.loaded_at.isoformat() if state.loaded_at else None,
            "poll_seconds": POLL_SECONDS,
            "error": state.error,
        }

@app.post("/api/refresh")
def api_refresh() -> dict[str, Any]:
    changed = refresh_volume()
    return {"changed": changed, **api_status()}

@app.get("/api/volume")
def api_volume() -> dict[str, Any]:
    radar = get_radar()
    fixed = np.asarray(radar.fixed_angle["data"], dtype=float)
    fields = [name for name in FIELD_CONFIG if name in radar.fields]
    sweeps = []
    for sweep in range(radar.nsweeps):
        start = int(radar.sweep_start_ray_index["data"][sweep])
        end = int(radar.sweep_end_ray_index["data"][sweep])
        sweeps.append({"index": sweep, "elevation": round(float(fixed[sweep]), 2), "rays": end - start + 1})
    with state_lock:
        key = state.key
        loaded_at = state.loaded_at
    return {
        "radar": RADAR_ID,
        "key": key,
        "loaded_at": loaded_at.isoformat() if loaded_at else None,
        "latitude": float(radar.latitude["data"][0]),
        "longitude": float(radar.longitude["data"][0]),
        "altitude_m": float(radar.altitude["data"][0]),
        "fields": [{"id": name, **FIELD_CONFIG[name]} for name in fields],
        "sweeps": sweeps,
    }

def _validate_field(radar, field: str, sweep: int) -> None:
    if field not in FIELD_CONFIG or field not in radar.fields:
        raise HTTPException(status_code=404, detail=f"Field '{field}' is not available.")
    if sweep < 0 or sweep >= radar.nsweeps:
        raise HTTPException(status_code=400, detail="Sweep index is out of range.")

@app.get("/api/image/{field}/{sweep}.png")
def api_image(field: str, sweep: int, range_km: float = Query(DEFAULT_RANGE_KM, ge=25, le=460)) -> Response:
    radar = get_radar()
    _validate_field(radar, field, sweep)
    gate_x, gate_y, _ = radar.get_gate_x_y_z(sweep)
    data = radar.get_field(sweep, field, copy=False)
    cfg = FIELD_CONFIG[field]
    fig = plt.figure(figsize=(10, 10), dpi=120)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.pcolormesh(gate_x / 1000.0, gate_y / 1000.0, data, shading="auto",
                  cmap=cfg["cmap"], vmin=cfg["vmin"], vmax=cfg["vmax"], rasterized=True)
    ax.set_xlim(-range_km, range_km)
    ax.set_ylim(-range_km, range_km)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    output = io.BytesIO()
    fig.savefig(output, format="png", dpi=120, facecolor="#090d14", edgecolor="none")
    plt.close(fig)
    return Response(output.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})

def _circular_diff(a: np.ndarray, b: float) -> np.ndarray:
    return np.abs((a - b + 180.0) % 360.0 - 180.0)

@app.get("/api/inspect")
def api_inspect(x_km: float = Query(..., ge=-460, le=460), y_km: float = Query(..., ge=-460, le=460)) -> dict[str, Any]:
    radar = get_radar()
    target_range_km = math.hypot(x_km, y_km)
    target_az = (math.degrees(math.atan2(x_km, y_km)) + 360.0) % 360.0
    ranges_km = np.asarray(radar.range["data"], dtype=float) / 1000.0
    gate_index = int(np.argmin(np.abs(ranges_km - target_range_km)))
    rows = []
    for sweep in range(radar.nsweeps):
        sweep_slice = radar.get_slice(sweep)
        azimuths = np.asarray(radar.azimuth["data"][sweep_slice], dtype=float)
        local_ray = int(np.argmin(_circular_diff(azimuths, target_az)))
        global_ray = int(radar.sweep_start_ray_index["data"][sweep]) + local_ray
        _, _, z = radar.get_gate_x_y_z(sweep)
        values: dict[str, float | None] = {}
        for field in FIELD_CONFIG:
            if field not in radar.fields:
                continue
            arr = radar.fields[field]["data"]
            val = arr[global_ray, gate_index]
            values[field] = None if np.ma.is_masked(val) else round(float(val), 3)
        rows.append({
            "sweep": sweep,
            "elevation": round(float(radar.fixed_angle["data"][sweep]), 2),
            "height_km": round(float(z[local_ray, gate_index]) / 1000.0, 3),
            "azimuth": round(float(azimuths[local_ray]), 2),
            "range_km": round(float(ranges_km[gate_index]), 2),
            "values": values,
        })
    return {
        "x_km": round(x_km, 2), "y_km": round(y_km, 2),
        "azimuth": round(target_az, 2), "range_km": round(target_range_km, 2),
        "rows": rows,
    }

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

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
import numpy as np
import pyart
from botocore import UNSIGNED
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from matplotlib import colormaps
from PIL import Image

RADAR_ID = os.getenv("RADAR_ID", "KMOB").upper()
BUCKET = os.getenv("NEXRAD_BUCKET", "unidata-nexrad-level2")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
DEFAULT_RANGE_KM = float(os.getenv("DEFAULT_RANGE_KM", "150"))
RASTER_SIZE = int(os.getenv("RADAR_RASTER_SIZE", "640"))
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
    volume_time: datetime | None = None
    loaded_at: datetime | None = None
    file_path: Path | None = None
    error: str | None = None


state = VolumeState()
state_lock = threading.RLock()
image_cache: dict[tuple[Any, ...], bytes] = {}
image_cache_lock = threading.RLock()
s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))
app = FastAPI(title="KMOB Level-II Volume Explorer", version="0.2.0")


def _candidate_prefixes(now: datetime) -> list[str]:
    """Query only the current/recent hours instead of listing the entire day."""
    prefixes: list[str] = []
    for hours_back in range(3):
        d = now - timedelta(hours=hours_back)
        prefixes.append(f"{d:%Y/%m/%d}/{RADAR_ID}/{RADAR_ID}{d:%Y%m%d_%H}")
    return prefixes


def _volume_time_from_key(key: str) -> datetime | None:
    name = key.rsplit("/", 1)[-1]
    try:
        stamp = name[len(RADAR_ID):len(RADAR_ID) + 15]
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def find_latest_key() -> str:
    candidates: list[tuple[datetime, str]] = []
    for prefix in _candidate_prefixes(datetime.now(timezone.utc)):
        response = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
        for obj in response.get("Contents", []):
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


def _clear_render_cache() -> None:
    with image_cache_lock:
        image_cache.clear()


def load_key(key: str) -> None:
    local_path = CACHE_DIR / key.rsplit("/", 1)[-1]
    if not local_path.exists():
        s3.download_file(BUCKET, key, str(local_path))

    radar = pyart.io.read_nexrad_archive(str(local_path), station=RADAR_ID)

    with state_lock:
        old_path = state.file_path
        state.radar = radar
        state.key = key
        state.volume_time = _volume_time_from_key(key)
        state.loaded_at = datetime.now(timezone.utc)
        state.file_path = local_path
        state.error = None

    _clear_render_cache()

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


def _state_snapshot() -> dict[str, Any]:
    with state_lock:
        now = datetime.now(timezone.utc)
        age_seconds = (now - state.volume_time).total_seconds() if state.volume_time else None
        return {
            "radar": RADAR_ID,
            "bucket": BUCKET,
            "loaded": state.radar is not None,
            "key": state.key,
            "volume_time": state.volume_time.isoformat() if state.volume_time else None,
            "loaded_at": state.loaded_at.isoformat() if state.loaded_at else None,
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "poll_seconds": POLL_SECONDS,
            "error": state.error,
        }


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return _state_snapshot()


@app.post("/api/refresh")
def api_refresh() -> dict[str, Any]:
    changed = refresh_volume()
    return {"changed": changed, **_state_snapshot()}


@app.get("/api/volume")
def api_volume() -> dict[str, Any]:
    radar = get_radar()
    fixed = np.asarray(radar.fixed_angle["data"], dtype=float)
    fields = [name for name in FIELD_CONFIG if name in radar.fields]
    sweeps = []

    for sweep in range(radar.nsweeps):
        start = int(radar.sweep_start_ray_index["data"][sweep])
        end = int(radar.sweep_end_ray_index["data"][sweep])
        sweeps.append({
            "index": sweep,
            "elevation": round(float(fixed[sweep]), 2),
            "rays": end - start + 1,
        })

    snap = _state_snapshot()
    return {
        "radar": RADAR_ID,
        "key": snap["key"],
        "volume_time": snap["volume_time"],
        "loaded_at": snap["loaded_at"],
        "age_seconds": snap["age_seconds"],
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


def _render_polar_png(radar, field: str, sweep: int, range_km: float, smooth: bool) -> bytes:
    with state_lock:
        volume_key = state.key

    cache_key = (volume_key, field, sweep, round(range_km, 1), smooth, RASTER_SIZE)
    with image_cache_lock:
        cached = image_cache.get(cache_key)
    if cached is not None:
        return cached

    data_ma = radar.get_field(sweep, field, copy=False)
    data = np.asarray(np.ma.filled(data_ma, np.nan), dtype=np.float32)

    sweep_slice = radar.get_slice(sweep)
    azimuths = np.asarray(radar.azimuth["data"][sweep_slice], dtype=np.float32)
    ranges_km = np.asarray(radar.range["data"], dtype=np.float32) / 1000.0

    order = np.argsort(azimuths)
    az_sorted = azimuths[order]
    data_sorted = data[order]

    axis = np.linspace(-range_km, range_km, RASTER_SIZE, dtype=np.float32)
    xx, yy = np.meshgrid(axis, axis[::-1])
    rr = np.hypot(xx, yy)
    az = (np.degrees(np.arctan2(xx, yy)) + 360.0) % 360.0

    gate_hi = np.searchsorted(ranges_km, rr, side="right")
    gate_hi = np.clip(gate_hi, 1, len(ranges_km) - 1)
    gate_lo = gate_hi - 1
    r0 = ranges_km[gate_lo]
    r1 = ranges_km[gate_hi]
    range_weight = np.divide(rr - r0, r1 - r0, out=np.zeros_like(rr), where=(r1 != r0))
    range_weight = np.clip(range_weight, 0.0, 1.0)

    az_ext = np.concatenate(([az_sorted[-1] - 360.0], az_sorted, [az_sorted[0] + 360.0]))
    sorted_ray_indices = np.arange(len(az_sorted), dtype=np.int32)
    ray_ext = np.concatenate(([sorted_ray_indices[-1]], sorted_ray_indices, [sorted_ray_indices[0]]))
    ray_hi_pos = np.searchsorted(az_ext, az, side="right")
    ray_hi_pos = np.clip(ray_hi_pos, 1, len(az_ext) - 1)
    ray_lo_pos = ray_hi_pos - 1

    az0 = az_ext[ray_lo_pos]
    az1 = az_ext[ray_hi_pos]
    az_weight = np.divide(az - az0, az1 - az0, out=np.zeros_like(az), where=(az1 != az0))
    az_weight = np.clip(az_weight, 0.0, 1.0)

    ray_lo = ray_ext[ray_lo_pos]
    ray_hi = ray_ext[ray_hi_pos]

    if smooth:
        v00 = data_sorted[ray_lo, gate_lo]
        v01 = data_sorted[ray_lo, gate_hi]
        v10 = data_sorted[ray_hi, gate_lo]
        v11 = data_sorted[ray_hi, gate_hi]

        w00 = (1.0 - az_weight) * (1.0 - range_weight)
        w01 = (1.0 - az_weight) * range_weight
        w10 = az_weight * (1.0 - range_weight)
        w11 = az_weight * range_weight

        numerator = np.zeros_like(rr, dtype=np.float32)
        denominator = np.zeros_like(rr, dtype=np.float32)
        for values, weights in ((v00, w00), (v01, w01), (v10, w10), (v11, w11)):
            valid = np.isfinite(values)
            numerator += np.where(valid, values, 0.0) * weights
            denominator += valid.astype(np.float32) * weights

        sampled = np.divide(
            numerator,
            denominator,
            out=np.full_like(numerator, np.nan),
            where=denominator > 0.0,
        )
    else:
        nearest_ray = np.where(az_weight < 0.5, ray_lo, ray_hi)
        nearest_gate = np.where(range_weight < 0.5, gate_lo, gate_hi)
        sampled = data_sorted[nearest_ray, nearest_gate]

    sampled = np.where(rr <= ranges_km[-1], sampled, np.nan)
    cfg = FIELD_CONFIG[field]
    norm = np.clip((sampled - cfg["vmin"]) / (cfg["vmax"] - cfg["vmin"]), 0.0, 1.0)
    rgba = colormaps[cfg["cmap"]](np.nan_to_num(norm, nan=0.0), bytes=True)
    missing = ~np.isfinite(sampled)
    rgba[missing, 0] = 9
    rgba[missing, 1] = 13
    rgba[missing, 2] = 20
    rgba[missing, 3] = 255

    output = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(output, format="PNG", compress_level=1)
    png = output.getvalue()

    with image_cache_lock:
        if len(image_cache) >= 96:
            image_cache.pop(next(iter(image_cache)))
        image_cache[cache_key] = png

    return png


@app.get("/api/image/{field}/{sweep}.png")
def api_image(
    field: str,
    sweep: int,
    range_km: float = Query(DEFAULT_RANGE_KM, ge=25, le=460),
    smooth: bool = Query(True),
) -> Response:
    radar = get_radar()
    _validate_field(radar, field, sweep)
    png = _render_polar_png(radar, field, sweep, range_km, smooth)

    with state_lock:
        volume_key = state.key or "unknown"

    return Response(
        png,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{volume_key}-{field}-{sweep}-{range_km}-{int(smooth)}"',
        },
    )


def _circular_diff(a: np.ndarray, b: float) -> np.ndarray:
    return np.abs((a - b + 180.0) % 360.0 - 180.0)


@app.get("/api/inspect")
def api_inspect(
    x_km: float = Query(..., ge=-460, le=460),
    y_km: float = Query(..., ge=-460, le=460),
) -> dict[str, Any]:
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
        "x_km": round(x_km, 2),
        "y_km": round(y_km, 2),
        "azimuth": round(target_az, 2),
        "range_km": round(target_range_km, 2),
        "rows": rows,
    }


static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

from __future__ import annotations

import asyncio
import io
import math
import os
import re
import tempfile
import threading
import urllib.request
import zipfile
from collections import OrderedDict
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

try:
    import xradar as xd
    XRADAR_AVAILABLE = tuple(int(p) for p in xd.__version__.split(".")[:2]) >= (0, 12)
except Exception:
    xd = None
    XRADAR_AVAILABLE = False

try:
    from cartopy.io import shapereader
    CARTOPY_AVAILABLE = True
except Exception:
    shapereader = None
    CARTOPY_AVAILABLE = False

RADAR_ID = os.getenv("RADAR_ID", "KMOB").upper()
BUCKET = os.getenv("NEXRAD_BUCKET", "unidata-nexrad-level2")
CHUNK_BUCKET = os.getenv("NEXRAD_CHUNK_BUCKET", "unidata-nexrad-level2-chunks")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "2"))
HISTORY_FRAMES = int(os.getenv("HISTORY_FRAMES", "10"))
DEFAULT_RANGE_KM = float(os.getenv("DEFAULT_RANGE_KM", "150"))
RASTER_SIZE = int(os.getenv("RADAR_RASTER_SIZE", "640"))
CACHE_DIR = Path(os.getenv("RADAR_CACHE_DIR", Path(tempfile.gettempdir()) / "kmob-level2"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
BOUNDARY_DIR = CACHE_DIR / "boundaries"
BOUNDARY_DIR.mkdir(parents=True, exist_ok=True)

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
archive_history: list[dict[str, Any]] = []
archive_radar_cache: OrderedDict[str, Any] = OrderedDict()
archive_lock = threading.RLock()

live_lock = threading.RLock()
live_prefix: str | None = None
live_chunk_bytes: dict[str, bytes] = {}
live_tree: Any | None = None
live_token: str | None = None
live_volume_time: datetime | None = None
live_updated_at: datetime | None = None
live_complete = False
live_error: str | None = None

boundary_cache: dict[str, Any] = {}
boundary_lock = threading.RLock()

s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))
s3_chunks = boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))
app = FastAPI(title="KMOB Level-II Volume Explorer", version="0.3.0")


def _candidate_prefixes(now: datetime) -> list[str]:
    """Return robust day/site prefixes, newest day first."""
    return [
        f"{now:%Y/%m/%d}/{RADAR_ID}/",
        f"{(now - timedelta(days=1)):%Y/%m/%d}/{RADAR_ID}/",
    ]


def _volume_time_from_key(key: str) -> datetime | None:
    name = key.rsplit("/", 1)[-1]
    try:
        stamp = name[len(RADAR_ID):len(RADAR_ID) + 15]
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def find_latest_key() -> str:
    now = datetime.now(timezone.utc)

    for prefix in _candidate_prefixes(now):
        candidates: list[tuple[datetime, str]] = []
        paginator = s3.get_paginator("list_objects_v2")

        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                name = key.rsplit("/", 1)[-1]

                if "_MDM" in name:
                    continue
                if not name.startswith(RADAR_ID):
                    continue
                if "_V06" not in name and not name.endswith(".gz"):
                    continue

                candidates.append((obj["LastModified"], key))

        if candidates:
            latest = max(candidates, key=lambda item: item[0])[1]
            print(f"[NEXRAD] latest {RADAR_ID}: {latest}", flush=True)
            return latest

    raise RuntimeError(
        f"No recent Level-II files found for {RADAR_ID} in the current or previous UTC day."
    )


def _clear_render_cache() -> None:
    with image_cache_lock:
        image_cache.clear()


def _is_archive_volume_key(key: str) -> bool:
    name = key.rsplit("/", 1)[-1]
    if "_MDM" in name or not name.startswith(RADAR_ID):
        return False
    return "_V06" in name or name.endswith(".gz")


def _scan_archive_history(limit: int = HISTORY_FRAMES) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    found: list[dict[str, Any]] = []

    for prefix in _candidate_prefixes(now):
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not _is_archive_volume_key(key):
                    continue
                found.append({
                    "key": key,
                    "last_modified": obj["LastModified"],
                    "volume_time": _volume_time_from_key(key),
                })

    found.sort(key=lambda item: item["last_modified"], reverse=True)
    dedup: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in found:
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        dedup.append(item)
        if len(dedup) >= limit:
            break
    return dedup


def _archive_local_path(key: str) -> Path:
    return CACHE_DIR / key.rsplit("/", 1)[-1]


def _load_archive_radar(key: str):
    with archive_lock:
        cached = archive_radar_cache.get(key)
        if cached is not None:
            archive_radar_cache.move_to_end(key)
            return cached

    local_path = _archive_local_path(key)
    if not local_path.exists():
        print(f"[ARCHIVE] downloading {key}", flush=True)
        s3.download_file(BUCKET, key, str(local_path))

    radar = pyart.io.read_nexrad_archive(str(local_path), station=RADAR_ID)

    with archive_lock:
        archive_radar_cache[key] = radar
        archive_radar_cache.move_to_end(key)
        while len(archive_radar_cache) > 3:
            archive_radar_cache.popitem(last=False)
    return radar


def refresh_archive_history() -> bool:
    global archive_history
    try:
        new_history = _scan_archive_history()
        if not new_history:
            raise RuntimeError(f"No recent Level-II files found for {RADAR_ID}.")

        old_key = archive_history[0]["key"] if archive_history else None
        archive_history = new_history
        newest = new_history[0]
        changed = newest["key"] != old_key

        with state_lock:
            need_radar = state.radar is None or state.key != newest["key"]

        if need_radar:
            radar = _load_archive_radar(newest["key"])
            with state_lock:
                state.radar = radar
                state.key = newest["key"]
                state.volume_time = newest["volume_time"]
                state.loaded_at = datetime.now(timezone.utc)
                state.file_path = _archive_local_path(newest["key"])
                state.error = None

        if changed:
            print(f"[ARCHIVE] newest {RADAR_ID}: {newest['key']}", flush=True)
            _clear_render_cache()

        return changed
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"[ARCHIVE ERROR] {message}", flush=True)
        with state_lock:
            state.error = message
        return False


def _angle_matches(a: float, b: float, tolerance: float = 0.06) -> bool:
    return abs(float(a) - float(b)) <= tolerance


def _group_base_tilts(items: list[tuple[Any, float]]) -> list[dict[str, Any]]:
    """Keep the first ascending occurrence of each elevation; ignore later SAILS/MRLE repeats.

    Consecutive same-angle raw sweeps remain grouped so split-cut moments can still be
    selected from the same physical base elevation.
    """
    logical: list[dict[str, Any]] = []
    seen_angles: list[float] = []
    i = 0

    while i < len(items):
        raw_id, elevation = items[i]
        raw_group = [raw_id]
        j = i + 1

        while j < len(items) and _angle_matches(items[j][1], elevation):
            raw_group.append(items[j][0])
            j += 1

        if not any(_angle_matches(elevation, seen) for seen in seen_angles):
            logical.append({
                "index": len(logical),
                "elevation": float(elevation),
                "raw": raw_group,
            })
            seen_angles.append(float(elevation))

        i = j

    return logical


def _archive_base_tilts(radar) -> list[dict[str, Any]]:
    fixed = np.asarray(radar.fixed_angle["data"], dtype=float)
    items = [(idx, float(elev)) for idx, elev in enumerate(fixed)]
    return _group_base_tilts(items)


XR_FIELD_MAP = {
    "reflectivity": ("DBZH", "DBZ", "REF"),
    "velocity": ("VRADH", "VRAD", "VEL"),
    "differential_reflectivity": ("ZDR",),
    "cross_correlation_ratio": ("RHOHV", "RHOHV_NC"),
    "differential_phase": ("PHIDP",),
    "spectrum_width": ("WRADH", "WIDTH"),
}


def _live_sweep_groups(tree=None) -> list[str]:
    tree = tree if tree is not None else live_tree
    if tree is None:
        return []

    groups = []
    for group in getattr(tree, "groups", ()):
        name = str(group)
        if name.startswith("/sweep_"):
            groups.append(name)

    def sort_key(name: str) -> int:
        match = re.search(r"sweep_(\d+)$", name)
        return int(match.group(1)) if match else 9999

    return sorted(groups, key=sort_key)


def _live_base_tilts(tree=None) -> list[dict[str, Any]]:
    tree = tree if tree is not None else live_tree
    items: list[tuple[str, float]] = []

    if tree is None:
        return []

    for group in _live_sweep_groups(tree):
        try:
            ds = tree[group].to_dataset(inherit="all_coords")
            elevation = float(np.asarray(ds["sweep_fixed_angle"].values).reshape(-1)[0])
            items.append((group, elevation))
        except Exception:
            continue

    return _group_base_tilts(items)


def _chunk_volume_prefix_and_keys() -> tuple[str | None, list[dict[str, Any]]]:
    if not XRADAR_AVAILABLE:
        return None, []

    root = f"{RADAR_ID}/"
    response = s3_chunks.list_objects_v2(
        Bucket=CHUNK_BUCKET,
        Prefix=root,
        Delimiter="/",
    )
    prefixes = [item["Prefix"] for item in response.get("CommonPrefixes", [])]

    candidates: list[tuple[datetime, str, list[dict[str, Any]]]] = []
    for prefix in prefixes:
        listing = s3_chunks.list_objects_v2(Bucket=CHUNK_BUCKET, Prefix=prefix)
        objects = listing.get("Contents", [])
        if not objects:
            continue
        newest = max(obj["LastModified"] for obj in objects)
        candidates.append((newest, prefix, objects))

    if not candidates:
        return None, []

    _, prefix, objects = max(candidates, key=lambda item: item[0])
    objects.sort(key=lambda obj: obj["Key"])
    return prefix, objects


def _chunk_time_from_key(key: str) -> datetime | None:
    name = key.rsplit("/", 1)[-1]
    match = re.search(r"(\d{8})-(\d{6})", name)
    if not match:
        return None
    try:
        return datetime.strptime(
            match.group(1) + match.group(2), "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def refresh_live_chunks() -> bool:
    global live_prefix, live_chunk_bytes, live_tree, live_token
    global live_volume_time, live_updated_at, live_complete, live_error

    if not XRADAR_AVAILABLE:
        with live_lock:
            live_error = "Xradar 0.12+ is required for tilt-as-it-arrives streaming."
        return False

    try:
        prefix, objects = _chunk_volume_prefix_and_keys()
        if not prefix or not objects:
            raise RuntimeError(f"No real-time chunk volume found for {RADAR_ID}.")

        with live_lock:
            if prefix != live_prefix:
                live_prefix = prefix
                live_chunk_bytes = {}
                live_tree = None
                live_token = None

        for obj in objects:
            key = obj["Key"]
            with live_lock:
                have_key = key in live_chunk_bytes
            if have_key:
                continue
            body = s3_chunks.get_object(Bucket=CHUNK_BUCKET, Key=key)["Body"].read()
            with live_lock:
                live_chunk_bytes[key] = body

        with live_lock:
            ordered_keys = sorted(live_chunk_bytes)
            candidate = [live_chunk_bytes[key] for key in ordered_keys]

        if not candidate:
            return False

        tree = xd.io.open_nexradlevel2_datatree(
            candidate,
            incomplete_sweep="pad",
        )
        groups = _live_sweep_groups(tree)
        if not groups:
            return False

        last_obj = max(objects, key=lambda obj: obj["LastModified"])
        token = f"{prefix}:{len(ordered_keys)}:{int(last_obj['LastModified'].timestamp())}"
        complete = any(re.search(r"-\d+-E$", key.rsplit("/", 1)[-1]) for key in ordered_keys)

        with live_lock:
            changed = token != live_token
            live_tree = tree
            live_token = token
            live_volume_time = _chunk_time_from_key(ordered_keys[0])
            live_updated_at = datetime.now(timezone.utc)
            live_complete = complete
            live_error = None

        if changed:
            print(
                f"[LIVE] {RADAR_ID} {len(ordered_keys)} chunks · "
                f"{len(_live_base_tilts(tree))} base tilts · {'complete' if complete else 'scanning'}",
                flush=True,
            )
            _clear_render_cache()

        return changed
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        with live_lock:
            live_error = message
        print(f"[LIVE ERROR] {message}", flush=True)
        return False


async def poll_loop() -> None:
    archive_counter = 0
    while True:
        await asyncio.to_thread(refresh_live_chunks)

        if archive_counter <= 0:
            await asyncio.to_thread(refresh_archive_history)
            archive_counter = max(1, int(10 / max(POLL_SECONDS, 1)))
        else:
            archive_counter -= 1

        await asyncio.sleep(POLL_SECONDS)


@app.on_event("startup")
async def on_startup() -> None:
    await asyncio.to_thread(refresh_archive_history)
    await asyncio.to_thread(refresh_live_chunks)
    asyncio.create_task(poll_loop())


def get_radar():
    with state_lock:
        radar = state.radar
    if radar is None:
        refresh_archive_history()
        with state_lock:
            radar = state.radar
    if radar is None:
        raise HTTPException(
            status_code=503,
            detail=state.error or "Radar volume is not loaded yet.",
        )
    return radar


def _live_root_values() -> tuple[float, float, float]:
    with live_lock:
        tree = live_tree
    if tree is None:
        raise RuntimeError("Live chunk volume is not available.")

    root = tree["/"].to_dataset()
    latitude = float(np.asarray(root["latitude"].values).reshape(-1)[0])
    longitude = float(np.asarray(root["longitude"].values).reshape(-1)[0])
    altitude = float(np.asarray(root["altitude"].values).reshape(-1)[0])
    return latitude, longitude, altitude


def _state_snapshot() -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    with state_lock:
        archive_age = (
            (now - state.volume_time).total_seconds()
            if state.volume_time else None
        )
        archive_loaded = state.radar is not None
        archive_error = state.error

    with live_lock:
        live_age = (
            (now - live_volume_time).total_seconds()
            if live_volume_time else None
        )
        live_available = live_tree is not None
        token = live_token
        updated = live_updated_at
        complete = live_complete
        error = live_error
        chunk_count = len(live_chunk_bytes)

    return {
        "radar": RADAR_ID,
        "bucket": BUCKET,
        "chunk_bucket": CHUNK_BUCKET,
        "loaded": live_available or archive_loaded,
        "archive_loaded": archive_loaded,
        "archive_key": state.key,
        "archive_volume_time": state.volume_time.isoformat() if state.volume_time else None,
        "archive_age_seconds": round(archive_age, 1) if archive_age is not None else None,
        "live_available": live_available,
        "live_token": token,
        "live_volume_time": live_volume_time.isoformat() if live_volume_time else None,
        "live_age_seconds": round(live_age, 1) if live_age is not None else None,
        "live_updated_at": updated.isoformat() if updated else None,
        "live_complete": complete,
        "live_chunk_count": chunk_count,
        "poll_seconds": POLL_SECONDS,
        "history_frames": len(archive_history),
        "xradar_available": XRADAR_AVAILABLE,
        "live_error": error,
        "error": archive_error,
    }


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return _state_snapshot()


@app.post("/api/refresh")
def api_refresh() -> dict[str, Any]:
    live_changed = refresh_live_chunks()
    archive_changed = refresh_archive_history()
    return {
        "changed": live_changed or archive_changed,
        "live_changed": live_changed,
        "archive_changed": archive_changed,
        **_state_snapshot(),
    }


@app.get("/api/history")
def api_history() -> dict[str, Any]:
    frames = [{
        "frame": 0,
        "source": "live" if live_tree is not None else "archive",
        "key": live_token if live_tree is not None else (archive_history[0]["key"] if archive_history else None),
        "volume_time": (
            live_volume_time.isoformat()
            if live_tree is not None and live_volume_time
            else (
                archive_history[0]["volume_time"].isoformat()
                if archive_history and archive_history[0]["volume_time"]
                else None
            )
        ),
        "label": "LIVE" if live_tree is not None else "LATEST",
    }]

    for idx, item in enumerate(archive_history[:HISTORY_FRAMES], start=1):
        frames.append({
            "frame": idx,
            "source": "archive",
            "key": item["key"],
            "volume_time": item["volume_time"].isoformat() if item["volume_time"] else None,
            "label": f"-{idx}",
        })

    return {"frames": frames, "max_frame": max(0, len(frames) - 1)}


def _archive_frame(frame: int) -> tuple[Any, dict[str, Any]]:
    if not archive_history:
        refresh_archive_history()

    archive_index = max(0, frame - 1)
    if frame == 0 and live_tree is None:
        archive_index = 0

    if archive_index >= len(archive_history):
        raise HTTPException(status_code=404, detail="History frame is not available.")

    entry = archive_history[archive_index]
    return _load_archive_radar(entry["key"]), entry


def _live_field_name(ds, field: str) -> str | None:
    for candidate in XR_FIELD_MAP.get(field, ()):
        if candidate in ds.data_vars:
            return candidate
    return None


def _live_tilt_completion(tilt: dict[str, Any]) -> float:
    with live_lock:
        tree = live_tree
    if tree is None:
        return 0.0

    best = 0.0
    for group in tilt["raw"]:
        try:
            ds = tree[group].to_dataset(inherit="all_coords")
        except Exception:
            continue

        for field in ("reflectivity", "velocity", "differential_reflectivity"):
            var_name = _live_field_name(ds, field)
            if not var_name:
                continue
            arr = np.asarray(ds[var_name].values)
            if arr.ndim < 2:
                continue
            if "range" in ds[var_name].dims:
                range_axis = ds[var_name].dims.index("range")
            else:
                range_axis = arr.ndim - 1
            valid_rays = np.any(np.isfinite(arr), axis=range_axis)
            pct = float(np.mean(valid_rays)) * 100.0
            best = max(best, pct)

    return min(100.0, best)


@app.get("/api/volume")
def api_volume(frame: int = Query(0, ge=0, le=HISTORY_FRAMES)) -> dict[str, Any]:
    if frame == 0:
        with live_lock:
            tree = live_tree

        if tree is not None:
            latitude, longitude, altitude = _live_root_values()
            tilts = _live_base_tilts(tree)
            fields: list[str] = []

            for field in FIELD_CONFIG:
                found = False
                for tilt in tilts:
                    for group in tilt["raw"]:
                        try:
                            ds = tree[group].to_dataset(inherit="all_coords")
                            if _live_field_name(ds, field):
                                found = True
                                break
                        except Exception:
                            pass
                    if found:
                        break
                if found:
                    fields.append(field)

            sweeps = [{
                "index": tilt["index"],
                "elevation": round(tilt["elevation"], 2),
                "completion": round(_live_tilt_completion(tilt), 1),
                "raw_count": len(tilt["raw"]),
            } for tilt in tilts]

            return {
                "radar": RADAR_ID,
                "source": "live",
                "key": live_token,
                "volume_time": live_volume_time.isoformat() if live_volume_time else None,
                "loaded_at": live_updated_at.isoformat() if live_updated_at else None,
                "age_seconds": _state_snapshot()["live_age_seconds"],
                "latitude": latitude,
                "longitude": longitude,
                "altitude_m": altitude,
                "fields": [{"id": name, **FIELD_CONFIG[name]} for name in fields],
                "sweeps": sweeps,
                "frame": 0,
                "live_complete": live_complete,
            }

    radar, entry = _archive_frame(frame)
    tilts = _archive_base_tilts(radar)
    fields = [name for name in FIELD_CONFIG if name in radar.fields]

    sweeps = [{
        "index": tilt["index"],
        "elevation": round(tilt["elevation"], 2),
        "completion": 100.0,
        "raw_count": len(tilt["raw"]),
    } for tilt in tilts]

    return {
        "radar": RADAR_ID,
        "source": "archive",
        "key": entry["key"],
        "volume_time": entry["volume_time"].isoformat() if entry["volume_time"] else None,
        "loaded_at": None,
        "age_seconds": (
            round((datetime.now(timezone.utc) - entry["volume_time"]).total_seconds(), 1)
            if entry["volume_time"] else None
        ),
        "latitude": float(radar.latitude["data"][0]),
        "longitude": float(radar.longitude["data"][0]),
        "altitude_m": float(radar.altitude["data"][0]),
        "fields": [{"id": name, **FIELD_CONFIG[name]} for name in fields],
        "sweeps": sweeps,
        "frame": frame,
        "live_complete": True,
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

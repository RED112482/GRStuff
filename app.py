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
archive_sequence_cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
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


def _bool_attr(ds, name: str) -> bool:
    value = ds.attrs.get(name, False)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    try:
        return bool(value)
    except Exception:
        return False


def _int_attr(ds, name: str) -> int:
    value = ds.attrs.get(name, 0)
    try:
        return int(value)
    except Exception:
        return 0


def _xradar_physical_sequence(tree) -> list[dict[str, Any]]:
    """Return physical scan cuts in acquisition order, preserving supplemental cuts.

    Consecutive same-angle split cuts are grouped into one physical elevation scan.
    SAILS/MRLE labels come from native Level-II metadata exposed by Xradar.
    """
    raw_items: list[dict[str, Any]] = []

    for group in _live_sweep_groups(tree):
        try:
            ds = tree[group].to_dataset(inherit="all_coords")
            elevation = float(np.asarray(ds["sweep_fixed_angle"].values).reshape(-1)[0])
        except Exception:
            continue

        raw_items.append({
            "group": group,
            "elevation": elevation,
            "sails": _bool_attr(ds, "sails_cut"),
            "sails_sequence": _int_attr(ds, "sails_sequence_number"),
            "mrle": _bool_attr(ds, "mrle_cut"),
            "mrle_sequence": _int_attr(ds, "mrle_sequence_number"),
            "base_tilt_cut": _bool_attr(ds, "base_tilt_cut"),
            "waveform_type": str(ds.attrs.get("waveform_type", "")),
        })

    scans: list[dict[str, Any]] = []
    i = 0
    while i < len(raw_items):
        item = raw_items[i]
        grouped = [item]
        j = i + 1

        while j < len(raw_items) and _angle_matches(
            raw_items[j]["elevation"], item["elevation"]
        ):
            grouped.append(raw_items[j])
            j += 1

        sails = any(x["sails"] for x in grouped)
        mrle = any(x["mrle"] for x in grouped)
        sails_seq = max((x["sails_sequence"] for x in grouped), default=0)
        mrle_seq = max((x["mrle_sequence"] for x in grouped), default=0)

        if sails:
            kind = "SAILS"
            sequence_number = sails_seq or 1
        elif mrle:
            kind = "MRLE"
            sequence_number = mrle_seq or 1
        else:
            kind = "BASE"
            sequence_number = 0

        scans.append({
            "sequence_index": len(scans),
            "elevation": float(item["elevation"]),
            "kind": kind,
            "sequence_number": sequence_number,
            "raw": [x["group"] for x in grouped],
            "split_cut": len(grouped) > 1,
            "base_tilt_cut": any(x["base_tilt_cut"] for x in grouped),
        })
        i = j

    return scans


def _base_tilts_from_sequence(sequence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    logical: list[dict[str, Any]] = []
    seen: list[float] = []

    for scan in sequence:
        if scan["kind"] != "BASE":
            continue
        elevation = float(scan["elevation"])
        if any(_angle_matches(elevation, value) for value in seen):
            continue
        logical.append({
            "index": len(logical),
            "elevation": elevation,
            "raw": scan["raw"],
            "sequence_index": scan["sequence_index"],
            "kind": "BASE",
        })
        seen.append(elevation)

    return logical


def _xradar_sequence_for_archive(key: str) -> list[dict[str, Any]]:
    if not XRADAR_AVAILABLE:
        return []

    with archive_lock:
        cached = archive_sequence_cache.get(key)
        if cached is not None:
            archive_sequence_cache.move_to_end(key)
            return cached

    local_path = _archive_local_path(key)
    if not local_path.exists():
        s3.download_file(BUCKET, key, str(local_path))

    try:
        tree = xd.io.open_nexradlevel2_datatree(
            str(local_path),
            incomplete_sweep="drop",
        )
        sequence = _xradar_physical_sequence(tree)
        with archive_lock:
            archive_sequence_cache[key] = sequence
            archive_sequence_cache.move_to_end(key)
            while len(archive_sequence_cache) > 5:
                archive_sequence_cache.popitem(last=False)
        return sequence
    except Exception as exc:
        print(f"[SEQUENCE TEMPLATE ERROR] {type(exc).__name__}: {exc}", flush=True)
        return []


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


def _live_scan_sequence(tree=None) -> list[dict[str, Any]]:
    tree = tree if tree is not None else live_tree
    if tree is None:
        return []
    return _xradar_physical_sequence(tree)


def _live_base_tilts(tree=None) -> list[dict[str, Any]]:
    return _base_tilts_from_sequence(_live_scan_sequence(tree))


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


def _history_frames_metadata() -> list[dict[str, Any]]:
    if not archive_history:
        refresh_archive_history()

    with live_lock:
        has_live = live_tree is not None
        token = live_token
        live_time = live_volume_time

    frames: list[dict[str, Any]] = []

    if has_live:
        frames.append({
            "frame": 0,
            "source": "live",
            "key": token,
            "archive_index": None,
            "volume_time": live_time.isoformat() if live_time else None,
            "label": "LIVE",
        })

        archive_items = list(enumerate(archive_history[:HISTORY_FRAMES]))
    else:
        if not archive_history:
            return []
        first = archive_history[0]
        frames.append({
            "frame": 0,
            "source": "archive",
            "key": first["key"],
            "archive_index": 0,
            "volume_time": first["volume_time"].isoformat() if first["volume_time"] else None,
            "label": "LATEST",
        })
        archive_items = list(enumerate(archive_history[1:HISTORY_FRAMES], start=1))

    for archive_index, item in archive_items:
        # Avoid showing the same volume twice when the live chunk volume has
        # already appeared in the completed-volume archive.
        if has_live and live_time and item["volume_time"]:
            if abs((item["volume_time"] - live_time).total_seconds()) < 30:
                continue

        frame_number = len(frames)
        frames.append({
            "frame": frame_number,
            "source": "archive",
            "key": item["key"],
            "archive_index": archive_index,
            "volume_time": item["volume_time"].isoformat() if item["volume_time"] else None,
            "label": f"-{frame_number}",
        })

        if frame_number >= HISTORY_FRAMES:
            break

    return frames


@app.get("/api/history")
def api_history() -> dict[str, Any]:
    frames = _history_frames_metadata()
    return {
        "frames": frames,
        "max_frame": max(0, len(frames) - 1),
        "count": len(frames),
    }


def _archive_frame(frame: int) -> tuple[Any, dict[str, Any]]:
    frames = _history_frames_metadata()
    if frame < 0 or frame >= len(frames):
        raise HTTPException(status_code=404, detail="History frame is not available.")

    meta = frames[frame]
    if meta["source"] == "live":
        raise HTTPException(status_code=400, detail="Frame 0 is the live chunk volume.")

    archive_index = meta.get("archive_index")
    if archive_index is None or archive_index >= len(archive_history):
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


def _archive_raw_sweep_for_field(radar, tilt: dict[str, Any], field: str) -> int:
    if field not in radar.fields:
        raise HTTPException(status_code=404, detail=f"Field '{field}' is not available.")

    for raw_sweep in tilt["raw"]:
        try:
            data = radar.get_field(raw_sweep, field, copy=False)
            if np.ma.count(data) > 0:
                return int(raw_sweep)
        except Exception:
            continue

    return int(tilt["raw"][0])


def _archive_tilt_arrays(
    radar,
    field: str,
    logical_sweep: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    tilts = _archive_base_tilts(radar)
    if logical_sweep < 0 or logical_sweep >= len(tilts):
        raise HTTPException(status_code=400, detail="Tilt index is out of range.")

    tilt = tilts[logical_sweep]
    raw_sweep = _archive_raw_sweep_for_field(radar, tilt, field)
    data_ma = radar.get_field(raw_sweep, field, copy=False)
    data = np.asarray(np.ma.filled(data_ma, np.nan), dtype=np.float32)
    sweep_slice = radar.get_slice(raw_sweep)
    azimuths = np.asarray(radar.azimuth["data"][sweep_slice], dtype=np.float32)
    ranges_km = np.asarray(radar.range["data"], dtype=np.float32) / 1000.0
    return data, azimuths, ranges_km, float(tilt["elevation"])


def _live_tilt_arrays(
    field: str,
    logical_sweep: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    with live_lock:
        tree = live_tree

    if tree is None:
        raise HTTPException(status_code=503, detail="Live chunk volume is not available.")

    tilts = _live_base_tilts(tree)
    if logical_sweep < 0 or logical_sweep >= len(tilts):
        raise HTTPException(status_code=400, detail="Tilt index is out of range.")

    tilt = tilts[logical_sweep]

    for group in tilt["raw"]:
        ds = tree[group].to_dataset(inherit="all_coords")
        var_name = _live_field_name(ds, field)
        if not var_name:
            continue

        da = ds[var_name]
        if "range" not in da.dims:
            continue

        if "azimuth" in da.dims:
            da = da.transpose("azimuth", "range")
            azimuths = np.asarray(ds["azimuth"].values, dtype=np.float32)
        elif "time" in da.dims and "azimuth" in ds.coords:
            da = da.transpose("time", "range")
            azimuths = np.asarray(ds["azimuth"].values, dtype=np.float32)
        else:
            continue

        data = np.asarray(da.values, dtype=np.float32)
        ranges_km = np.asarray(ds["range"].values, dtype=np.float32) / 1000.0

        if data.ndim == 2 and data.shape[0] == len(azimuths):
            return data, azimuths, ranges_km, float(tilt["elevation"])

    raise HTTPException(
        status_code=404,
        detail=f"Field '{field}' is not available for this tilt yet.",
    )


def _render_array_png(
    data: np.ndarray,
    azimuths: np.ndarray,
    ranges_km: np.ndarray,
    field: str,
    range_km: float,
    smooth: bool,
    size: int,
    cache_key: tuple[Any, ...],
) -> bytes:
    with image_cache_lock:
        cached = image_cache.get(cache_key)
    if cached is not None:
        return cached

    if field not in FIELD_CONFIG:
        raise HTTPException(status_code=404, detail=f"Unknown field '{field}'.")

    data = np.asarray(data, dtype=np.float32)
    azimuths = np.asarray(azimuths, dtype=np.float32)
    ranges_km = np.asarray(ranges_km, dtype=np.float32)

    if data.ndim != 2 or len(azimuths) != data.shape[0] or len(ranges_km) != data.shape[1]:
        raise HTTPException(status_code=500, detail="Unexpected radar array geometry.")

    order = np.argsort(azimuths)
    az_sorted = azimuths[order]
    data_sorted = data[order]

    axis = np.linspace(-range_km, range_km, size, dtype=np.float32)
    xx, yy = np.meshgrid(axis, axis[::-1])
    rr = np.hypot(xx, yy)
    az = (np.degrees(np.arctan2(xx, yy)) + 360.0) % 360.0

    gate_hi = np.searchsorted(ranges_km, rr, side="right")
    gate_hi = np.clip(gate_hi, 1, len(ranges_km) - 1)
    gate_lo = gate_hi - 1
    r0 = ranges_km[gate_lo]
    r1 = ranges_km[gate_hi]
    range_weight = np.divide(
        rr - r0,
        r1 - r0,
        out=np.zeros_like(rr),
        where=(r1 != r0),
    )
    range_weight = np.clip(range_weight, 0.0, 1.0)

    az_ext = np.concatenate((
        [az_sorted[-1] - 360.0],
        az_sorted,
        [az_sorted[0] + 360.0],
    ))
    sorted_ray_indices = np.arange(len(az_sorted), dtype=np.int32)
    ray_ext = np.concatenate((
        [sorted_ray_indices[-1]],
        sorted_ray_indices,
        [sorted_ray_indices[0]],
    ))

    ray_hi_pos = np.searchsorted(az_ext, az, side="right")
    ray_hi_pos = np.clip(ray_hi_pos, 1, len(az_ext) - 1)
    ray_lo_pos = ray_hi_pos - 1

    az0 = az_ext[ray_lo_pos]
    az1 = az_ext[ray_hi_pos]
    az_weight = np.divide(
        az - az0,
        az1 - az0,
        out=np.zeros_like(az),
        where=(az1 != az0),
    )
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

        for values, weights in (
            (v00, w00),
            (v01, w01),
            (v10, w10),
            (v11, w11),
        ):
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
    norm = np.clip(
        (sampled - cfg["vmin"]) / (cfg["vmax"] - cfg["vmin"]),
        0.0,
        1.0,
    )
    rgba = colormaps[cfg["cmap"]](np.nan_to_num(norm, nan=0.0), bytes=True)
    missing = ~np.isfinite(sampled)
    rgba[missing, 0] = 9
    rgba[missing, 1] = 13
    rgba[missing, 2] = 20
    rgba[missing, 3] = 255

    output = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(
        output,
        format="PNG",
        compress_level=1,
    )
    png = output.getvalue()

    with image_cache_lock:
        if len(image_cache) >= 160:
            image_cache.pop(next(iter(image_cache)))
        image_cache[cache_key] = png

    return png


@app.get("/api/image/{field}/{sweep}.png")
def api_image(
    field: str,
    sweep: int,
    range_km: float = Query(DEFAULT_RANGE_KM, ge=25, le=460),
    smooth: bool = Query(True),
    frame: int = Query(0, ge=0, le=HISTORY_FRAMES),
    size: int = Query(RASTER_SIZE, ge=180, le=900),
) -> Response:
    if field not in FIELD_CONFIG:
        raise HTTPException(status_code=404, detail=f"Unknown field '{field}'.")

    source_key: str

    if frame == 0:
        with live_lock:
            has_live = live_tree is not None
            token = live_token

        if has_live:
            data, azimuths, ranges_km, _ = _live_tilt_arrays(field, sweep)
            source_key = token or "live"
            cache_key = (
                "live",
                source_key,
                field,
                sweep,
                round(range_km, 1),
                smooth,
                size,
            )
        else:
            radar, entry = _archive_frame(0)
            data, azimuths, ranges_km, _ = _archive_tilt_arrays(radar, field, sweep)
            source_key = entry["key"]
            cache_key = (
                "archive",
                source_key,
                field,
                sweep,
                round(range_km, 1),
                smooth,
                size,
            )
    else:
        radar, entry = _archive_frame(frame)
        data, azimuths, ranges_km, _ = _archive_tilt_arrays(radar, field, sweep)
        source_key = entry["key"]
        cache_key = (
            "archive",
            source_key,
            field,
            sweep,
            round(range_km, 1),
            smooth,
            size,
        )

    png = _render_array_png(
        data,
        azimuths,
        ranges_km,
        field,
        range_km,
        smooth,
        size,
        cache_key,
    )

    return Response(
        png,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{source_key}-{field}-{sweep}-{range_km}-{int(smooth)}-{size}"',
        },
    )


def _circular_diff(a: np.ndarray, b: float) -> np.ndarray:
    return np.abs((a - b + 180.0) % 360.0 - 180.0)


def _beam_height_km(range_km: float, elevation_deg: float) -> float:
    effective_earth_radius_km = 6371.0 * 4.0 / 3.0
    theta = math.radians(elevation_deg)
    slant_km = range_km / max(math.cos(theta), 0.02)
    return math.sqrt(
        slant_km * slant_km
        + effective_earth_radius_km * effective_earth_radius_km
        + 2.0 * slant_km * effective_earth_radius_km * math.sin(theta)
    ) - effective_earth_radius_km


def _sample_polar(
    data: np.ndarray,
    azimuths: np.ndarray,
    ranges_km: np.ndarray,
    target_az: float,
    target_range_km: float,
) -> float | None:
    if data.size == 0:
        return None

    ray = int(np.argmin(_circular_diff(np.asarray(azimuths, dtype=float), target_az)))
    gate = int(np.argmin(np.abs(np.asarray(ranges_km, dtype=float) - target_range_km)))
    value = data[ray, gate]

    if not np.isfinite(value):
        return None
    return round(float(value), 3)


def _live_inspect(x_km: float, y_km: float) -> dict[str, Any]:
    target_range_km = math.hypot(x_km, y_km)
    target_az = (math.degrees(math.atan2(x_km, y_km)) + 360.0) % 360.0
    tilts = _live_base_tilts()
    rows = []

    for tilt in tilts:
        values: dict[str, float | None] = {}
        actual_range = target_range_km
        actual_az = target_az

        for field in FIELD_CONFIG:
            try:
                data, azimuths, ranges_km, _ = _live_tilt_arrays(field, tilt["index"])
            except HTTPException:
                continue
            values[field] = _sample_polar(
                data,
                azimuths,
                ranges_km,
                target_az,
                target_range_km,
            )
            if len(ranges_km):
                actual_range = float(
                    ranges_km[int(np.argmin(np.abs(ranges_km - target_range_km)))]
                )
            if len(azimuths):
                actual_az = float(
                    azimuths[int(np.argmin(_circular_diff(azimuths, target_az)))]
                )

        rows.append({
            "sweep": tilt["index"],
            "elevation": round(float(tilt["elevation"]), 2),
            "height_km": round(_beam_height_km(actual_range, tilt["elevation"]), 3),
            "azimuth": round(actual_az, 2),
            "range_km": round(actual_range, 2),
            "completion": round(_live_tilt_completion(tilt), 1),
            "values": values,
        })

    return {
        "source": "live",
        "x_km": round(x_km, 2),
        "y_km": round(y_km, 2),
        "azimuth": round(target_az, 2),
        "range_km": round(target_range_km, 2),
        "rows": rows,
    }


def _archive_inspect(radar, x_km: float, y_km: float) -> dict[str, Any]:
    target_range_km = math.hypot(x_km, y_km)
    target_az = (math.degrees(math.atan2(x_km, y_km)) + 360.0) % 360.0
    ranges_km = np.asarray(radar.range["data"], dtype=float) / 1000.0
    gate_index = int(np.argmin(np.abs(ranges_km - target_range_km)))
    tilts = _archive_base_tilts(radar)
    rows = []

    for tilt in tilts:
        height_raw = int(tilt["raw"][0])
        height_slice = radar.get_slice(height_raw)
        height_az = np.asarray(radar.azimuth["data"][height_slice], dtype=float)
        height_local_ray = int(np.argmin(_circular_diff(height_az, target_az)))
        _, _, z = radar.get_gate_x_y_z(height_raw)
        height_km = float(z[height_local_ray, gate_index]) / 1000.0

        values: dict[str, float | None] = {}
        actual_az = target_az

        for field in FIELD_CONFIG:
            if field not in radar.fields:
                continue

            raw_sweep = _archive_raw_sweep_for_field(radar, tilt, field)
            sweep_slice = radar.get_slice(raw_sweep)
            azimuths = np.asarray(radar.azimuth["data"][sweep_slice], dtype=float)
            local_ray = int(np.argmin(_circular_diff(azimuths, target_az)))
            global_ray = int(radar.sweep_start_ray_index["data"][raw_sweep]) + local_ray
            val = radar.fields[field]["data"][global_ray, gate_index]
            values[field] = None if np.ma.is_masked(val) else round(float(val), 3)
            actual_az = float(azimuths[local_ray])

        rows.append({
            "sweep": tilt["index"],
            "elevation": round(float(tilt["elevation"]), 2),
            "height_km": round(height_km, 3),
            "azimuth": round(actual_az, 2),
            "range_km": round(float(ranges_km[gate_index]), 2),
            "completion": 100.0,
            "values": values,
        })

    return {
        "source": "archive",
        "x_km": round(x_km, 2),
        "y_km": round(y_km, 2),
        "azimuth": round(target_az, 2),
        "range_km": round(target_range_km, 2),
        "rows": rows,
    }


@app.get("/api/inspect")
def api_inspect(
    x_km: float = Query(..., ge=-460, le=460),
    y_km: float = Query(..., ge=-460, le=460),
    frame: int = Query(0, ge=0, le=HISTORY_FRAMES),
) -> dict[str, Any]:
    if frame == 0:
        with live_lock:
            has_live = live_tree is not None
        if has_live:
            result = _live_inspect(x_km, y_km)
            result["frame"] = 0
            return result

    radar, _ = _archive_frame(frame)
    result = _archive_inspect(radar, x_km, y_km)
    result["frame"] = frame
    return result


BOUNDARY_URLS = {
    "state": "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_5m.zip",
    "county": "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_county_5m.zip",
}


def _boundary_shapefile(layer: str) -> Path:
    if layer not in BOUNDARY_URLS:
        raise ValueError(f"Unknown boundary layer: {layer}")
    if not CARTOPY_AVAILABLE:
        raise RuntimeError("Cartopy is not available.")

    layer_dir = BOUNDARY_DIR / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    existing = list(layer_dir.glob("*.shp"))
    if existing:
        return existing[0]

    zip_path = BOUNDARY_DIR / f"{layer}.zip"
    if not zip_path.exists():
        print(f"[BOUNDARIES] downloading Census {layer} outlines", flush=True)
        urllib.request.urlretrieve(BOUNDARY_URLS[layer], zip_path)

    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(layer_dir)

    shapefiles = list(layer_dir.glob("*.shp"))
    if not shapefiles:
        raise RuntimeError(f"Census {layer} shapefile did not extract correctly.")
    return shapefiles[0]


def _iter_geometry_lines(geometry):
    geom_type = getattr(geometry, "geom_type", "")

    if geom_type == "Polygon":
        yield list(geometry.exterior.coords)
    elif geom_type == "MultiPolygon":
        for polygon in geometry.geoms:
            yield list(polygon.exterior.coords)
    elif geom_type == "LineString":
        yield list(geometry.coords)
    elif geom_type == "MultiLineString":
        for line in geometry.geoms:
            yield list(line.coords)


def _project_boundary_layer(
    layer: str,
    radar_lat: float,
    radar_lon: float,
    max_range_km: float,
) -> list[list[list[float]]]:
    cache_key = f"{layer}:{radar_lat:.3f}:{radar_lon:.3f}:{max_range_km:.0f}"

    with boundary_lock:
        cached = boundary_cache.get(cache_key)
    if cached is not None:
        return cached

    shp = _boundary_shapefile(layer)
    reader = shapereader.Reader(str(shp))

    degree_pad = max_range_km / 85.0 + 1.0
    lon_min = radar_lon - degree_pad
    lon_max = radar_lon + degree_pad
    lat_min = radar_lat - degree_pad
    lat_max = radar_lat + degree_pad

    segments: list[list[list[float]]] = []

    for geometry in reader.geometries():
        gx0, gy0, gx1, gy1 = geometry.bounds
        if gx1 < lon_min or gx0 > lon_max or gy1 < lat_min or gy0 > lat_max:
            continue

        for coords in _iter_geometry_lines(geometry):
            if len(coords) < 2:
                continue

            lon = np.asarray([point[0] for point in coords], dtype=float)
            lat = np.asarray([point[1] for point in coords], dtype=float)
            x, y = pyart.core.geographic_to_cartesian_aeqd(
                lon,
                lat,
                radar_lon,
                radar_lat,
            )
            x = np.asarray(x, dtype=float) / 1000.0
            y = np.asarray(y, dtype=float) / 1000.0

            keep = (
                (x >= -max_range_km * 1.15)
                & (x <= max_range_km * 1.15)
                & (y >= -max_range_km * 1.15)
                & (y <= max_range_km * 1.15)
            )
            if not np.any(keep):
                continue

            step = max(1, int(len(x) / 500))
            segment = [
                [round(float(px), 2), round(float(py), 2)]
                for px, py in zip(x[::step], y[::step])
            ]
            if len(segment) >= 2:
                segments.append(segment)

    with boundary_lock:
        boundary_cache[cache_key] = segments
    return segments


@app.get("/api/boundaries")
def api_boundaries(
    range_km: float = Query(320.0, ge=50, le=500),
) -> dict[str, Any]:
    try:
        if live_tree is not None:
            radar_lat, radar_lon, _ = _live_root_values()
        else:
            radar = get_radar()
            radar_lat = float(radar.latitude["data"][0])
            radar_lon = float(radar.longitude["data"][0])

        states = _project_boundary_layer("state", radar_lat, radar_lon, range_km)
        counties = _project_boundary_layer("county", radar_lat, radar_lon, range_km)

        return {
            "radar_lat": radar_lat,
            "radar_lon": radar_lon,
            "range_km": range_km,
            "states": states,
            "counties": counties,
        }
    except Exception as exc:
        print(f"[BOUNDARY ERROR] {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(
            status_code=503,
            detail=f"Boundary data unavailable: {exc}",
        )


static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

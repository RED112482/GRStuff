# Native KMOB radar desktop prototype\nfrom __future__ import annotations

import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import numpy as np
import shapefile
import xradar as xd
from pyproj import CRS, Transformer
from botocore import UNSIGNED
from botocore.config import Config

# Lock PyQtGraph to PySide6 before importing pyqtgraph.  This avoids binding
# auto-detection ambiguity and surfaces the real Qt import error on Windows.
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg


RADAR_ID = os.getenv("RADAR_ID", "KMOB").upper()
ARCHIVE_BUCKET = os.getenv("NEXRAD_BUCKET", "unidata-nexrad-level2")
CHUNK_BUCKET = os.getenv("NEXRAD_CHUNK_BUCKET", "unidata-nexrad-level2-chunks")
TGFTP_BASE = os.getenv(
    "NEXRAD_TGFTP_BASE",
    "https://tgftp.nws.noaa.gov/data/radar/nexrad_level2",
)
POLL_SECONDS = float(os.getenv("DESKTOP_POLL_SECONDS", "2"))
DEFAULT_RANGE_KM = float(os.getenv("DEFAULT_RANGE_KM", "150"))
HISTORY_PER_ELEVATION = int(os.getenv("DESKTOP_HISTORY_SCANS", "14"))
ARCHIVE_VOLUMES = int(os.getenv("DESKTOP_ARCHIVE_VOLUMES", "3"))

CACHE_DIR = Path(
    os.getenv(
        "RADAR_CACHE_DIR",
        Path(tempfile.gettempdir()) / "kmob-native-radar",
    )
)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
BOUNDARY_DIR = CACHE_DIR / "boundaries"
BOUNDARY_DIR.mkdir(parents=True, exist_ok=True)

BOUNDARY_URLS = {
    "state": "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_5m.zip",
    "county": "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_county_5m.zip",
}

pg.setConfigOption("imageAxisOrder", "row-major")
pg.setConfigOption("antialias", False)
pg.setConfigOption("background", (5, 8, 13))
pg.setConfigOption("foreground", (195, 205, 220))
try:
    pg.setConfigOption("useNumba", True)
except Exception:
    pass


@dataclass
class RadarScan:
    scan_id: str
    elevation: float
    scan_time: datetime
    source: str
    volume_id: str
    kind: str
    sequence_number: int
    sequence_index: int
    azimuth: np.ndarray
    range_km: np.ndarray
    reflectivity: np.ndarray
    completion: float

    @property
    def label(self) -> str:
        if self.kind in {"SAILS", "MRLE"}:
            return f"{self.kind} #{self.sequence_number} · {self.elevation:.2f}°"
        return f"{self.elevation:.2f}°"


def _bool_attr(ds, name: str) -> bool:
    value = ds.attrs.get(name, False)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    try:
        return bool(value)
    except Exception:
        return False


def _int_attr(ds, name: str) -> int:
    try:
        return int(ds.attrs.get(name, 0))
    except Exception:
        return 0


def _utc_from_np(value: np.datetime64 | Any) -> datetime:
    try:
        seconds = value.astype("datetime64[s]").astype(np.int64)
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _scan_time(ds) -> datetime:
    try:
        times = np.asarray(ds["time"].values).reshape(-1)
        if times.size:
            valid = times[~np.isnat(times)]
            if valid.size:
                return _utc_from_np(valid[0])
    except Exception:
        pass
    return datetime.now(timezone.utc)


def _reflectivity_name(ds) -> str | None:
    for name in ("DBZH", "DBZ", "REF", "reflectivity"):
        if name in ds.data_vars:
            return name
    return None


def _sweep_groups(tree) -> list[str]:
    groups = [
        str(group)
        for group in getattr(tree, "groups", ())
        if str(group).startswith("/sweep_")
    ]

    def key(name: str) -> int:
        match = re.search(r"sweep_(\d+)$", name)
        return int(match.group(1)) if match else 9999

    return sorted(groups, key=key)


def _extract_scans(tree, volume_id: str, source: str) -> list[RadarScan]:
    scans: list[RadarScan] = []

    for sequence_index, group in enumerate(_sweep_groups(tree)):
        try:
            ds = tree[group].to_dataset(inherit="all_coords")
            field_name = _reflectivity_name(ds)
            if field_name is None:
                continue

            da = ds[field_name]
            if "range" not in da.dims:
                continue

            if "azimuth" in da.dims:
                da = da.transpose("azimuth", "range")
            elif "time" in da.dims:
                da = da.transpose("time", "range")
            else:
                continue

            data = np.asarray(da.values, dtype=np.float32)
            azimuth = np.asarray(ds["azimuth"].values, dtype=np.float32)
            range_km = np.asarray(ds["range"].values, dtype=np.float32) / 1000.0
            elevation = float(
                np.asarray(ds["sweep_fixed_angle"].values).reshape(-1)[0]
            )

            if data.ndim != 2:
                continue
            if data.shape[0] != azimuth.size or data.shape[1] != range_km.size:
                continue

            sails = _bool_attr(ds, "sails_cut")
            mrle = _bool_attr(ds, "mrle_cut")
            if sails:
                kind = "SAILS"
                number = _int_attr(ds, "sails_sequence_number") or 1
            elif mrle:
                kind = "MRLE"
                number = _int_attr(ds, "mrle_sequence_number") or 1
            else:
                kind = "BASE"
                number = 0

            ray_valid = np.any(np.isfinite(data), axis=1)
            completion = float(np.mean(ray_valid)) * 100.0

            scans.append(
                RadarScan(
                    scan_id=f"{source}:{volume_id}:{group}",
                    elevation=elevation,
                    scan_time=_scan_time(ds),
                    source=source,
                    volume_id=volume_id,
                    kind=kind,
                    sequence_number=number,
                    sequence_index=sequence_index,
                    azimuth=np.ascontiguousarray(azimuth),
                    range_km=np.ascontiguousarray(range_km),
                    reflectivity=np.ascontiguousarray(data),
                    completion=completion,
                )
            )
        except Exception:
            continue

    scans.sort(key=lambda scan: scan.sequence_index)
    return scans


def _tree_site_location(tree) -> tuple[float, float]:
    root = tree["/"].to_dataset()
    lat = float(np.asarray(root["latitude"].values).reshape(-1)[0])
    lon = float(np.asarray(root["longitude"].values).reshape(-1)[0])
    return lat, lon


def _ensure_boundary_shapefile(kind: str) -> Path:
    target_dir = BOUNDARY_DIR / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    shp_files = list(target_dir.glob("*.shp"))
    if shp_files:
        return shp_files[0]

    zip_path = BOUNDARY_DIR / f"{kind}.zip"
    urllib.request.urlretrieve(BOUNDARY_URLS[kind], zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(target_dir)

    shp_files = list(target_dir.glob("*.shp"))
    if not shp_files:
        raise RuntimeError(f"No {kind} shapefile found after extraction.")
    return shp_files[0]


def _boundary_xy(
    kind: str,
    radar_lat: float,
    radar_lon: float,
    max_range_km: float = 420.0,
) -> tuple[np.ndarray, np.ndarray]:
    shp_path = _ensure_boundary_shapefile(kind)
    reader = shapefile.Reader(str(shp_path))

    local_crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={radar_lat} +lon_0={radar_lon} "
        "+datum=WGS84 +units=m +no_defs"
    )
    transformer = Transformer.from_crs(
        "EPSG:4326",
        local_crs,
        always_xy=True,
    )

    xs: list[float] = []
    ys: list[float] = []
    limit_m = max_range_km * 1000.0

    for shape in reader.shapes():
        points = shape.points
        if not points:
            continue
        parts = list(shape.parts) + [len(points)]
        for start, stop in zip(parts[:-1], parts[1:]):
            segment = points[start:stop]
            if len(segment) < 2:
                continue

            lon = np.asarray([p[0] for p in segment], dtype=np.float64)
            lat = np.asarray([p[1] for p in segment], dtype=np.float64)
            x_m, y_m = transformer.transform(lon, lat)

            x_m = np.asarray(x_m)
            y_m = np.asarray(y_m)
            keep = (
                (x_m >= -limit_m)
                & (x_m <= limit_m)
                & (y_m >= -limit_m)
                & (y_m <= limit_m)
            )
            if not np.any(keep):
                continue

            xs.extend((x_m / 1000.0).tolist())
            ys.extend((y_m / 1000.0).tolist())
            xs.append(np.nan)
            ys.append(np.nan)

    return (
        np.ascontiguousarray(xs, dtype=np.float32),
        np.ascontiguousarray(ys, dtype=np.float32),
    )


def _scan_strategy(tree) -> dict[str, Any]:
    try:
        root = tree["/"].to_dataset()
        attrs = root.attrs
    except Exception:
        return {
            "scan_name": "VCP ?",
            "dynamic_scan_type": "unknown",
            "number_elevation_cuts": 0,
            "vcp_sequence_active": False,
        }

    return {
        "scan_name": str(attrs.get("scan_name", "VCP ?")),
        "dynamic_scan_type": str(attrs.get("dynamic_scan_type", "standard")),
        "number_elevation_cuts": int(attrs.get("number_elevation_cuts", 0) or 0),
        "vcp_sequence_active": bool(attrs.get("vcp_sequence_active", False)),
    }


def _canonical_elevation(existing: list[float], value: float) -> float:
    for current in existing:
        if abs(current - value) <= 0.07:
            return current
    return round(value, 2)


def reflectivity_lut() -> np.ndarray:
    stops = [
        (-10, (5, 8, 13)),
        (0, (70, 70, 70)),
        (5, (4, 233, 231)),
        (15, (1, 159, 244)),
        (20, (2, 253, 2)),
        (30, (1, 197, 1)),
        (40, (255, 255, 0)),
        (50, (253, 149, 0)),
        (55, (255, 0, 0)),
        (60, (212, 0, 0)),
        (65, (255, 0, 255)),
        (70, (153, 85, 201)),
        (80, (255, 255, 255)),
    ]
    xs = np.array([item[0] for item in stops], dtype=np.float32)
    rgb = np.array([item[1] for item in stops], dtype=np.float32)
    target = np.linspace(-10, 80, 256, dtype=np.float32)
    lut = np.empty((256, 3), dtype=np.uint8)
    for channel in range(3):
        lut[:, channel] = np.interp(target, xs, rgb[:, channel]).astype(np.uint8)
    return lut


REF_LUT = reflectivity_lut()

_RENDER_GRID_CACHE: OrderedDict[
    tuple[float, float, float, float, int, int],
    tuple[np.ndarray, np.ndarray],
] = OrderedDict()
_SCAN_SORT_CACHE: dict[str, tuple[np.ndarray, np.ndarray]] = {}
_RENDER_CACHE_LOCK = threading.RLock()


def _render_grid(
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    key = (
        round(float(x_range[0]), 3),
        round(float(x_range[1]), 3),
        round(float(y_range[0]), 3),
        round(float(y_range[1]), 3),
        int(width),
        int(height),
    )

    # All wall panes normally share this exact geometry.  Keep the first
    # build inside the lock so parallel workers do not manufacture the same
    # mesh 16 times; subsequent workers immediately reuse the cached arrays.
    with _RENDER_CACHE_LOCK:
        cached = _RENDER_GRID_CACHE.get(key)
        if cached is not None:
            _RENDER_GRID_CACHE.move_to_end(key)
            return cached

        xs = np.linspace(x_range[0], x_range[1], width, dtype=np.float32)
        ys = np.linspace(y_range[0], y_range[1], height, dtype=np.float32)
        xx, yy = np.meshgrid(xs, ys)
        rr = np.hypot(xx, yy).astype(np.float32)
        az = ((np.degrees(np.arctan2(xx, yy)) + 360.0) % 360.0).astype(
            np.float32
        )

        _RENDER_GRID_CACHE[key] = (rr, az)
        _RENDER_GRID_CACHE.move_to_end(key)
        while len(_RENDER_GRID_CACHE) > 12:
            _RENDER_GRID_CACHE.popitem(last=False)
        return rr, az


def render_scan(
    scan: RadarScan,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    width: int,
    height: int,
) -> np.ndarray:
    width = max(120, min(int(width), 1100))
    height = max(120, min(int(height), 1100))

    xmin, xmax = x_range
    ymin, ymax = y_range
    rr, az = _render_grid(
        (xmin, xmax),
        (ymin, ymax),
        width,
        height,
    )

    ranges = scan.range_km
    if ranges.size < 2 or scan.azimuth.size < 2:
        return np.full((height, width), np.nan, dtype=np.float32)

    with _RENDER_CACHE_LOCK:
        cached_sort = _SCAN_SORT_CACHE.get(scan.scan_id)
    if cached_sort is None:
        order = np.argsort(scan.azimuth)
        az_sorted = np.ascontiguousarray(scan.azimuth[order])
        with _RENDER_CACHE_LOCK:
            _SCAN_SORT_CACHE[scan.scan_id] = (order, az_sorted)
    else:
        order, az_sorted = cached_sort

    data_sorted = scan.reflectivity[order]

    # Fast NEXRAD path: operational sweeps normally have nearly uniform
    # azimuth spacing (360/720 rays) and uniform range-gate spacing.  Direct
    # arithmetic avoids multiple searchsorted passes over every output pixel.
    az_steps = np.diff(az_sorted)
    az_step = float(np.nanmedian(az_steps)) if az_steps.size else 0.0
    range_steps = np.diff(ranges)
    range_step = (
        float(np.nanmedian(range_steps))
        if range_steps.size
        else 0.0
    )

    az_regular = (
        az_step > 0.0
        and np.nanmax(np.abs(az_steps - az_step)) <= max(0.08, az_step * 0.25)
    )
    range_regular = (
        range_step > 0.0
        and np.nanmax(np.abs(range_steps - range_step))
        <= max(0.002, range_step * 0.02)
    )

    if az_regular:
        ray_sorted = np.rint(
            (az - float(az_sorted[0])) / az_step
        ).astype(np.int32)
        ray_sorted %= az_sorted.size
    else:
        az_ext = np.concatenate(
            (
                [az_sorted[-1] - 360.0],
                az_sorted,
                [az_sorted[0] + 360.0],
            )
        )
        idx_ext = np.concatenate(
            (
                [len(az_sorted) - 1],
                np.arange(len(az_sorted), dtype=np.int32),
                [0],
            )
        )
        hi = np.searchsorted(az_ext, az, side="left")
        hi = np.clip(hi, 1, len(az_ext) - 1)
        lo = hi - 1
        choose_hi = np.abs(az - az_ext[hi]) < np.abs(az - az_ext[lo])
        ray_sorted = np.where(choose_hi, idx_ext[hi], idx_ext[lo])

    if range_regular:
        gate = np.rint(
            (rr - float(ranges[0])) / range_step
        ).astype(np.int32)
        gate = np.clip(gate, 0, ranges.size - 1)
    else:
        gate = np.searchsorted(ranges, rr, side="left")
        gate = np.clip(gate, 0, ranges.size - 1)
        lower_gate = np.maximum(gate - 1, 0)
        choose_lower = (
            np.abs(rr - ranges[lower_gate])
            < np.abs(rr - ranges[gate])
        )
        gate = np.where(choose_lower, lower_gate, gate)

    sampled = np.asarray(
        data_sorted[ray_sorted, gate],
        dtype=np.float32,
    )
    sampled[rr > ranges[-1]] = np.nan
    return np.ascontiguousarray(sampled)



class RenderTaskSignals(QtCore.QObject):
    finished = QtCore.Signal(object, object, object)
    failed = QtCore.Signal(object, str)


class RenderTask(QtCore.QRunnable):
    def __init__(
        self,
        signature: tuple[Any, ...],
        scan: RadarScan,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
        width: int,
        height: int,
    ):
        super().__init__()
        self.signature = signature
        self.scan = scan
        self.x_range = x_range
        self.y_range = y_range
        self.width = width
        self.height = height
        self.signals = RenderTaskSignals()

    @QtCore.Slot()
    def run(self):
        try:
            image = render_scan(
                self.scan,
                self.x_range,
                self.y_range,
                self.width,
                self.height,
            )
            rect = (
                self.x_range[0],
                self.y_range[0],
                self.x_range[1] - self.x_range[0],
                self.y_range[1] - self.y_range[0],
            )
            self.signals.finished.emit(self.signature, image, rect)
        except Exception as exc:
            self.signals.failed.emit(
                self.signature,
                f"{type(exc).__name__}: {exc}",
            )


class RadarDataWorker(QtCore.QThread):
    snapshot = QtCore.Signal(object)
    status = QtCore.Signal(str)
    boundaries = QtCore.Signal(object)

    def __init__(self):
        super().__init__()
        self._running = True
        self.histories: dict[float, list[RadarScan]] = defaultdict(list)
        self.current_live_volume: str | None = None
        self.live_sequence: list[RadarScan] = []
        self.expected_sequence: list[RadarScan] = []
        self.strategy: dict[str, Any] = {
            "scan_name": "VCP ?",
            "dynamic_scan_type": "unknown",
            "number_elevation_cuts": 0,
            "vcp_sequence_active": False,
        }
        self.live_complete = False
        self.site_lat: float | None = None
        self.site_lon: float | None = None
        self._boundaries_loaded = False
        self._chunk_bytes: dict[str, bytes] = {}

        self.archive_s3 = boto3.client(
            "s3",
            region_name="us-east-1",
            config=Config(
                signature_version=UNSIGNED,
                connect_timeout=3,
                read_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
        self.chunk_s3 = boto3.client(
            "s3",
            region_name="us-east-1",
            config=Config(
                signature_version=UNSIGNED,
                connect_timeout=3,
                read_timeout=6,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )

    def stop(self):
        self._running = False

    def run(self):
        self.status.emit("Loading recent completed volume…")
        try:
            self._bootstrap_archive()
        except Exception as exc:
            self.status.emit(f"Archive bootstrap: {type(exc).__name__}: {exc}")

        while self._running:
            try:
                changed = self._refresh_live()
                if changed:
                    self._emit_snapshot()
            except Exception as exc:
                self.status.emit(f"Live feed: {type(exc).__name__}: {exc}")

            for _ in range(max(1, int(POLL_SECONDS * 10))):
                if not self._running:
                    break
                self.msleep(100)

    def _recent_archive_keys(self) -> list[str]:
        """Find recent completed volumes without scanning a full radar day."""
        now = datetime.now(timezone.utc)
        found: dict[str, datetime] = {}

        # Completed Level-II filenames begin with KMOBYYYYMMDD_HH, so querying
        # only the last few UTC hours keeps startup listings tiny.
        for hours_back in range(0, 5):
            stamp = now - timedelta(hours=hours_back)
            prefix = (
                f"{stamp:%Y/%m/%d}/{RADAR_ID}/"
                f"{RADAR_ID}{stamp:%Y%m%d_%H}"
            )
            response = self.archive_s3.list_objects_v2(
                Bucket=ARCHIVE_BUCKET,
                Prefix=prefix,
                MaxKeys=1000,
            )
            for obj in response.get("Contents", []):
                key = obj["Key"]
                name = key.rsplit("/", 1)[-1]
                if "_MDM" in name:
                    continue
                if "_V06" not in name and not name.endswith(".gz"):
                    continue
                found[key] = obj["LastModified"]

            if len(found) >= ARCHIVE_VOLUMES:
                break

        ordered = sorted(
            found.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        return [key for key, _ in ordered[:ARCHIVE_VOLUMES]]


    def _recent_tgftp_files(self) -> list[str]:
        url = f"{TGFTP_BASE}/{RADAR_ID}/dir.list"
        with urllib.request.urlopen(url, timeout=6) as response:
            text = response.read().decode("utf-8", errors="replace")

        names: list[str] = []
        for line in text.splitlines():
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            name = parts[-1]
            if name.startswith(f"{RADAR_ID}_") and name.endswith(".bz2"):
                names.append(name)

        # Filename contains YYYYMMDD_HHMMSS, so lexical order is chronological
        # regardless of how dir.list happens to be ordered by the server.
        names = sorted(set(names))
        return names[-ARCHIVE_VOLUMES:]

    def _tgftp_content_length(self, name: str) -> int | None:
        url = f"{TGFTP_BASE}/{RADAR_ID}/{name}"
        request = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                value = response.headers.get("Content-Length")
                return int(value) if value else None
        except Exception:
            return None

    def _download_tgftp_file(self, name: str) -> Path:
        path = CACHE_DIR / name
        expected_size = self._tgftp_content_length(name)

        if path.exists() and path.stat().st_size > 0:
            local_size = path.stat().st_size
            if expected_size is None or local_size == expected_size:
                return path
            # Cached file was captured while the upstream file was still
            # growing, or otherwise does not match the current server object.
            try:
                path.unlink()
            except OSError:
                pass

        url = f"{TGFTP_BASE}/{RADAR_ID}/{name}"
        tmp = path.with_suffix(path.suffix + ".part")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

        with urllib.request.urlopen(url, timeout=20) as response:
            response_size = response.headers.get("Content-Length")
            response_size = int(response_size) if response_size else expected_size
            with open(tmp, "wb") as handle:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    handle.write(block)

        actual_size = tmp.stat().st_size
        if response_size is not None and actual_size != response_size:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise IOError(
                f"Incomplete NWS Level-II download for {name}: "
                f"{actual_size} of {response_size} bytes"
            )

        tmp.replace(path)
        return path


    def _archive_path(self, key: str) -> Path:
        return CACHE_DIR / key.rsplit("/", 1)[-1]

    def _bootstrap_archive(self):
        loaded_any = False

        # Primary startup/history source: completed AWS Level-II volumes.
        # Hour-scoped listing above makes this fast while avoiding partially
        # written tgftp files.
        try:
            keys = self._recent_archive_keys()
            for idx, key in enumerate(reversed(keys)):
                if not self._running:
                    return

                path = self._archive_path(key)
                if not path.exists():
                    self.status.emit(
                        f"Downloading completed Level-II "
                        f"{idx + 1}/{len(keys)}…"
                    )
                    self.archive_s3.download_file(
                        ARCHIVE_BUCKET,
                        key,
                        str(path),
                    )

                try:
                    tree = xd.io.open_nexradlevel2_datatree(
                        str(path),
                        incomplete_sweep="pad",
                    )
                except Exception as exc:
                    print(
                        f"[AWS HISTORY SKIP] {key}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    continue

                scans = _extract_scans(tree, key, "archive")
                if not scans:
                    continue

                if self.site_lat is None or self.site_lon is None:
                    try:
                        self.site_lat, self.site_lon = _tree_site_location(tree)
                    except Exception:
                        pass

                self._merge_scans(scans)
                self.expected_sequence = list(scans)
                self.strategy = _scan_strategy(tree)
                loaded_any = True

        except Exception as exc:
            print(
                f"[AWS HISTORY ERROR] {type(exc).__name__}: {exc}",
                flush=True,
            )

        # Secondary fallback only. tgftp is useful, but its newest file can be
        # observed while still growing, so do not make it the startup gate.
        if not loaded_any:
            try:
                names = self._recent_tgftp_files()
                for idx, name in enumerate(names):
                    if not self._running:
                        return

                    self.status.emit(
                        f"Loading NWS fallback history "
                        f"{idx + 1}/{len(names)}…"
                    )
                    path = self._download_tgftp_file(name)
                    try:
                        tree = xd.io.open_nexradlevel2_datatree(
                            str(path),
                            incomplete_sweep="pad",
                        )
                    except Exception as exc:
                        print(
                            f"[NWS HISTORY SKIP] {name}: "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        continue

                    scans = _extract_scans(tree, name, "archive")
                    if not scans:
                        continue

                    if self.site_lat is None or self.site_lon is None:
                        try:
                            self.site_lat, self.site_lon = _tree_site_location(tree)
                        except Exception:
                            pass

                    self._merge_scans(scans)
                    self.expected_sequence = list(scans)
                    self.strategy = _scan_strategy(tree)
                    loaded_any = True
            except Exception as exc:
                print(
                    f"[NWS HISTORY ERROR] {type(exc).__name__}: {exc}",
                    flush=True,
                )

        if loaded_any:
            total_scans = sum(len(items) for items in self.histories.values())
            self.status.emit(
                f"History ready · {total_scans} scans · connecting live chunks…"
            )
            self._emit_snapshot()
            self._load_boundaries()
        else:
            self.status.emit(
                "No decoded history yet · waiting for live chunks…"
            )
            self._emit_snapshot()


    def _load_boundaries(self):
        if self._boundaries_loaded:
            return
        if self.site_lat is None or self.site_lon is None:
            return

        try:
            self.status.emit("Loading state/county outlines…")
            state_xy = _boundary_xy(
                "state",
                self.site_lat,
                self.site_lon,
            )
            county_xy = _boundary_xy(
                "county",
                self.site_lat,
                self.site_lon,
            )
            self.boundaries.emit(
                {
                    "state": state_xy,
                    "county": county_xy,
                }
            )
            self._boundaries_loaded = True
        except Exception as exc:
            self.status.emit(
                f"Boundary load: {type(exc).__name__}: {exc}"
            )


    def _chunk_prefixes(self, limit: int = 5) -> list[str]:
        root = f"{RADAR_ID}/"
        response = self.chunk_s3.list_objects_v2(
            Bucket=CHUNK_BUCKET,
            Prefix=root,
            Delimiter="/",
            MaxKeys=1000,
        )
        prefixes = [item["Prefix"] for item in response.get("CommonPrefixes", [])]

        def prefix_key(prefix: str):
            leaf = prefix.rstrip("/").rsplit("/", 1)[-1]
            try:
                return int(leaf)
            except ValueError:
                return -1

        prefixes.sort(key=prefix_key, reverse=True)
        return prefixes[:limit]

    def _chunk_objects(self, prefix: str) -> list[dict[str, Any]]:
        response = self.chunk_s3.list_objects_v2(
            Bucket=CHUNK_BUCKET,
            Prefix=prefix,
            MaxKeys=1000,
        )
        return response.get("Contents", [])

    def _chunk_volume_candidates(
        self,
        objects: list[dict[str, Any]],
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        """Group the rolling station directory into real radar volumes.

        The numeric S3 directory can contain chunks from multiple volume
        timestamps.  Xradar requires exactly one volume, with its S chunk
        first, followed by that volume's I/E chunks in numeric order.
        """
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for obj in objects:
            name = obj["Key"].rsplit("/", 1)[-1]
            match = re.match(
                r"^(?P<volume>\d{8}-\d{6})-"
                r"(?P<sequence>\d+)-(?P<kind>[SIE])$",
                name,
            )
            if match is None:
                continue

            item = dict(obj)
            item["_volume_id"] = match.group("volume")
            item["_sequence"] = int(match.group("sequence"))
            item["_kind"] = match.group("kind")
            groups[item["_volume_id"]].append(item)

        candidates: list[tuple[str, list[dict[str, Any]]]] = []
        for volume_id, items in groups.items():
            items.sort(key=lambda item: item["_sequence"])
            if not items:
                continue

            s_positions = [
                index
                for index, item in enumerate(items)
                if item["_kind"] == "S"
            ]
            if not s_positions:
                continue

            # Drop any stale I/E chunks that precede this volume's start chunk.
            start = s_positions[0]
            items = items[start:]
            if not items or items[0]["_kind"] != "S":
                continue
            candidates.append((volume_id, items))

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates

    def _refresh_live(self) -> bool:
        prefixes = self._chunk_prefixes(limit=5)
        if not prefixes:
            return False

        all_candidates: list[
            tuple[str, str, list[dict[str, Any]]]
        ] = []
        for prefix in prefixes:
            try:
                objects = self._chunk_objects(prefix)
            except Exception:
                continue
            for volume_id, volume_objects in self._chunk_volume_candidates(objects):
                all_candidates.append((volume_id, prefix, volume_objects))

        if not all_candidates:
            self.status.emit("LIVE · waiting for a volume-start S chunk…")
            return False

        # A valid volume timestamp is sortable as text (YYYYMMDD-HHMMSS).
        all_candidates.sort(key=lambda item: item[0], reverse=True)

        for volume_id, prefix, volume_objects in all_candidates[:6]:
            is_current = volume_id == self.current_live_volume
            chunk_cache = self._chunk_bytes if is_current else {}
            changed = not is_current

            for obj in volume_objects:
                key = obj["Key"]
                if key in chunk_cache:
                    continue
                chunk_cache[key] = self.chunk_s3.get_object(
                    Bucket=CHUNK_BUCKET,
                    Key=key,
                )["Body"].read()
                changed = True

            ordered = [
                obj for obj in volume_objects
                if obj["Key"] in chunk_cache
            ]
            if not ordered:
                continue

            first_bytes = chunk_cache[ordered[0]["Key"]]
            if not first_bytes[:4].startswith(b"AR2V"):
                continue

            if not changed:
                return False

            chunks = [chunk_cache[obj["Key"]] for obj in ordered]
            try:
                tree = xd.io.open_nexradlevel2_datatree(
                    chunks,
                    incomplete_sweep="pad",
                )
            except (ValueError, EOFError, OSError):
                continue

            scans = _extract_scans(tree, volume_id, "live")
            if not scans:
                # Keep completed/archive display until the new live volume has
                # enough bytes to expose its first partial sweep.
                continue

            self.current_live_volume = volume_id
            self._chunk_bytes = chunk_cache
            self.live_sequence = scans

            live_strategy = _scan_strategy(tree)
            if live_strategy.get("scan_name") != "VCP ?":
                self.strategy = live_strategy

            self.live_complete = any(
                obj["_kind"] == "E"
                for obj in ordered
            )
            self._merge_scans(scans)

            last = scans[-1]
            self.status.emit(
                f"LIVE · {last.label} · {last.completion:.0f}% "
                f"· {len(chunks)} chunks"
            )
            return True

        self.status.emit(
            "LIVE · no decodable S→I/E volume yet · showing completed data"
        )
        return False

    def _merge_scans(self, scans: list[RadarScan]):
        canonical_keys = list(self.histories.keys())
        for scan in scans:
            key = _canonical_elevation(canonical_keys, scan.elevation)
            if key not in self.histories:
                canonical_keys.append(key)

            history = self.histories[key]
            existing = next(
                (i for i, item in enumerate(history) if item.scan_id == scan.scan_id),
                None,
            )
            if existing is None:
                history.append(scan)
            else:
                history[existing] = scan

            history.sort(key=lambda item: item.scan_time, reverse=True)
            del history[HISTORY_PER_ELEVATION:]

    def _emit_snapshot(self):
        copied = {
            key: list(value)
            for key, value in self.histories.items()
        }

        scanning_scan = None
        if not self.live_complete:
            if self.live_sequence and self.live_sequence[-1].completion < 98.0:
                scanning_scan = self.live_sequence[-1]
            elif len(self.live_sequence) < len(self.expected_sequence):
                scanning_scan = self.expected_sequence[len(self.live_sequence)]

        self.snapshot.emit(
            {
                "histories": copied,
                "live_sequence": list(self.live_sequence),
                "live_volume": self.current_live_volume,
                "strategy": dict(self.strategy),
                "scanning_scan": scanning_scan,
                "live_complete": self.live_complete,
            }
        )


class RadarViewBox(pg.ViewBox):
    activated = QtCore.Signal()

    def __init__(self):
        super().__init__(
            lockAspect=True,
            enableMenu=False,
            defaultPadding=0.0,
        )
        self.setMouseMode(pg.ViewBox.PanMode)
        self.setLimits(
            xMin=-500,
            xMax=500,
            yMin=-500,
            yMax=500,
            minXRange=1.0,
            minYRange=1.0,
        )

    def set_interaction_mode(self, mode: str):
        if mode == "zoom":
            self.setMouseMode(pg.ViewBox.RectMode)
        else:
            self.setMouseMode(pg.ViewBox.PanMode)


    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.activated.emit()
        super().mouseClickEvent(ev)

    def mouseDragEvent(self, ev, axis=None):
        super().mouseDragEvent(ev, axis=axis)

    def mouseDoubleClickEvent(self, ev):
        self.autoRange(padding=0.0)
        ev.accept()


class RadarCanvas(QtWidgets.QWidget):
    activated = QtCore.Signal(object)
    range_changed = QtCore.Signal(object)

    def __init__(self, compact: bool = False):
        super().__init__()
        self.compact = compact
        self.scan: RadarScan | None = None
        self.is_latest = False
        self.is_scanning = False
        self._rerendering = False
        self._last_render_signature: tuple[Any, ...] | None = None
        self._pending_render_signature: tuple[Any, ...] | None = None

        self.view_box = RadarViewBox()
        self.plot = pg.PlotWidget(viewBox=self.view_box)
        self.plot.setMenuEnabled(False)
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.plot.setAspectLocked(True)
        self.plot.setBackground((5, 8, 13))

        self.image = pg.ImageItem(axisOrder="row-major")
        self.image.setLookupTable(REF_LUT)
        self.image.setLevels((-10, 80))
        self.image.setAutoDownsample(True)
        self.view_box.addItem(self.image)

        self.county_item = pg.PlotDataItem(
            pen=pg.mkPen((225, 230, 238, 95), width=0.7),
            connect="finite",
            antialias=False,
        )
        self.state_item = pg.PlotDataItem(
            pen=pg.mkPen((255, 255, 255, 205), width=1.35),
            connect="finite",
            antialias=False,
        )
        self.county_item.setZValue(20)
        self.state_item.setZValue(21)
        self.view_box.addItem(self.county_item)
        self.view_box.addItem(self.state_item)

        self.title = QtWidgets.QLabel("—")
        self.title.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.title.setStyleSheet(
            "QLabel { background: rgba(4,8,13,190); color: white; "
            "padding: 4px 6px; border-radius: 4px; font-weight: 700; }"
        )

        self.badge = QtWidgets.QLabel("")
        self.badge.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.badge.setStyleSheet(
            "QLabel { background: rgba(4,8,13,210); color: #9fb0c5; "
            "padding: 4px 6px; border-radius: 4px; font-weight: 700; }"
        )

        self.time_badge = QtWidgets.QLabel("—")
        self.time_badge.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.time_badge.setStyleSheet(
            "QLabel { background: rgba(4,8,13,210); color: #eef4fb; "
            "padding: 4px 6px; border-radius: 4px; font-weight: 700; }"
        )

        self.scan_badge = QtWidgets.QLabel("")
        self.scan_badge.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.scan_badge.setStyleSheet(
            "QLabel { background: rgba(4,8,13,220); color: #ffb547; "
            "padding: 4px 6px; border-radius: 4px; font-weight: 800; }"
        )
        self.scan_badge.hide()

        overlay = QtWidgets.QGridLayout()
        overlay.setContentsMargins(6, 6, 6, 6)
        overlay.addWidget(self.title, 0, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        overlay.addWidget(self.badge, 0, 1, QtCore.Qt.AlignmentFlag.AlignRight)
        overlay.setRowStretch(1, 1)
        overlay.addWidget(
            self.time_badge,
            2,
            0,
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignBottom,
        )
        overlay.addWidget(
            self.scan_badge,
            2,
            1,
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignBottom,
        )

        stack = QtWidgets.QStackedLayout(self)
        stack.setStackingMode(QtWidgets.QStackedLayout.StackingMode.StackAll)
        stack.addWidget(self.plot)

        self.overlay_widget = QtWidgets.QWidget()
        self.overlay_widget.setLayout(overlay)
        self.overlay_widget.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        stack.addWidget(self.overlay_widget)
        # In StackAll mode the current widget is raised above the others.
        # Make the status overlay explicitly topmost so labels never sit
        # behind the PlotWidget/OpenGL paint surface.
        stack.setCurrentWidget(self.overlay_widget)
        self.overlay_widget.raise_()

        self.view_box.activated.connect(lambda: self.activated.emit(self))
        self.view_box.sigRangeChanged.connect(
            lambda *_: self.range_changed.emit(self)
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "overlay_widget"):
            self.overlay_widget.raise_()

    def set_boundaries(self, data: dict[str, tuple[np.ndarray, np.ndarray]]):
        county = data.get("county")
        state = data.get("state")
        if county is not None:
            self.county_item.setData(
                county[0],
                county[1],
                connect="finite",
            )
        if state is not None:
            self.state_item.setData(
                state[0],
                state[1],
                connect="finite",
            )


    def set_scan(
        self,
        scan: RadarScan | None,
        current_live_volume: str | None,
        is_latest: bool = False,
        is_scanning: bool = False,
    ):
        prior_id = self.scan.scan_id if self.scan is not None else None
        self.scan = scan
        self.is_latest = bool(is_latest)
        self.is_scanning = bool(is_scanning)

        if scan is None:
            self.title.setText("—")
            self.badge.setText("NO SCAN")
            self.time_badge.setText("—")
            self.scan_badge.hide()
            self.image.clear()
            self._last_render_signature = None
            self._pending_render_signature = None
            return

        self.title.setText(
            f"{scan.elevation:.2f}°"
            + (
                f" · {scan.kind} #{scan.sequence_number}"
                if scan.kind != "BASE"
                else " · BASE"
            )
        )
        self.time_badge.setText(scan.scan_time.strftime("%H:%M:%SZ"))
        if prior_id != scan.scan_id:
            self._last_render_signature = None
        self.update_age_badge(current_live_volume)

    def update_age_badge(self, current_live_volume: str | None):
        scan = self.scan
        if scan is None:
            return

        age_seconds = max(
            0,
            int((datetime.now(timezone.utc) - scan.scan_time).total_seconds()),
        )
        if age_seconds < 60:
            age_text = f"{age_seconds}s"
        elif age_seconds < 3600:
            age_text = f"{age_seconds // 60}m {age_seconds % 60:02d}s"
        else:
            age_text = (
                f"{age_seconds // 3600}h "
                f"{(age_seconds % 3600) // 60:02d}m"
            )

        if self.is_scanning:
            status = "● SCANNING"
            color = "#ffb547"
            self.scan_badge.setText("SCANNING…")
            self.scan_badge.show()
        elif self.is_latest:
            status = "● LATEST"
            color = "#35d07f"
            self.scan_badge.hide()
        else:
            status = "● OLDER"
            color = "#ff5b69"
            self.scan_badge.hide()

        live_suffix = ""
        if scan.source == "live" and scan.volume_id == current_live_volume:
            live_suffix = " · LIVE"

        self.badge.setText(f"{status} · {age_text}{live_suffix}")
        self.badge.setStyleSheet(
            "QLabel { background: rgba(4,8,13,220); "
            f"color: {color}; padding: 4px 6px; border-radius: 4px; "
            "font-weight: 800; }"
        )


    def set_interaction_mode(self, mode: str):
        self.view_box.set_interaction_mode(mode)


    def set_active(self, active: bool):
        self.setStyleSheet(
            "RadarCanvas { border: 2px solid #4ea1ff; }"
            if active
            else "RadarCanvas { border: 1px solid #1d2633; }"
        )

    def visible_ranges(self) -> tuple[tuple[float, float], tuple[float, float]]:
        x_range, y_range = self.view_box.viewRange()
        return (tuple(x_range), tuple(y_range))

    def set_view_ranges(
        self,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ):
        self._rerendering = True
        self.view_box.setRange(
            xRange=x_range,
            yRange=y_range,
            padding=0.0,
        )
        self._rerendering = False

    def reset_view(self):
        self.set_view_ranges(
            (-DEFAULT_RANGE_KM, DEFAULT_RANGE_KM),
            (-DEFAULT_RANGE_KM, DEFAULT_RANGE_KM),
        )

    def _render_spec(self):
        if self.scan is None:
            return None

        x_range, y_range = self.visible_ranges()
        size = self.plot.viewport().size()
        if self.compact:
            width = max(120, min(size.width(), 160))
            height = max(120, min(size.height(), 160))
        else:
            width = max(350, min(size.width(), 1000))
            height = max(350, min(size.height(), 1000))

        signature = (
            self.scan.scan_id,
            round(float(self.scan.completion), 1),
            round(float(x_range[0]), 3),
            round(float(x_range[1]), 3),
            round(float(y_range[0]), 3),
            round(float(y_range[1]), 3),
            int(width),
            int(height),
        )
        return signature, x_range, y_range, width, height

    def cancel_pending_render(self):
        self._pending_render_signature = None

    def request_rerender(
        self,
        pool: QtCore.QThreadPool,
        priority: int = 0,
    ):
        spec = self._render_spec()
        if spec is None:
            return

        signature, x_range, y_range, width, height = spec
        if signature == self._last_render_signature:
            return
        if signature == self._pending_render_signature:
            return

        self._pending_render_signature = signature
        task = RenderTask(
            signature,
            self.scan,
            x_range,
            y_range,
            width,
            height,
        )
        task.signals.finished.connect(self._apply_render_result)
        task.signals.failed.connect(self._render_failed)
        pool.start(task, priority)

    @QtCore.Slot(object, object, object)
    def _apply_render_result(self, signature, image, rect):
        if signature != self._pending_render_signature:
            return
        self.image.setImage(
            image,
            autoLevels=False,
            levels=(-10, 80),
        )
        self.image.setRect(QtCore.QRectF(*rect))
        self._last_render_signature = signature
        self._pending_render_signature = None

    @QtCore.Slot(object, str)
    def _render_failed(self, signature, message: str):
        if signature == self._pending_render_signature:
            self._pending_render_signature = None
        print(f"[RENDER ERROR] {message}", flush=True)




class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KMOB Native Radar Analyst")
        self.resize(1500, 980)

        self.histories: dict[float, list[RadarScan]] = {}
        self.live_sequence: list[RadarScan] = []
        self.live_volume: str | None = None
        self.strategy: dict[str, Any] = {
            "scan_name": "VCP ?",
            "dynamic_scan_type": "unknown",
        }
        self.scanning_scan: RadarScan | None = None
        self.live_complete = False
        self.interaction_mode = "pan"
        self.boundary_data: dict[str, tuple[np.ndarray, np.ndarray]] | None = None

        self.anchor_elevation: float | None = None
        self.anchor_index = 0
        self.wall_scans: dict[float, RadarScan | None] = {}
        self.follow_live = True

        self._syncing_ranges = False
        self._wall_mode = False

        self.render_pool = QtCore.QThreadPool(self)
        cpu_count = os.cpu_count() or 4
        self.render_pool.setMaxThreadCount(max(2, min(6, cpu_count - 1)))

        # Range-change signals can fire while widgets are being constructed,
        # so create the debounce timer before _build_ui() connects any view.
        self.render_timer = QtCore.QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(100)
        self.render_timer.timeout.connect(self._rerender_visible)

        self.age_timer = QtCore.QTimer(self)
        self.age_timer.setInterval(1000)
        self.age_timer.timeout.connect(self._refresh_age_labels)
        self.age_timer.start()

        self._build_ui()

        self.worker = RadarDataWorker()
        self.worker.snapshot.connect(self._apply_snapshot)
        self.worker.status.connect(self._set_status)
        self.worker.boundaries.connect(self._apply_boundaries)
        self.worker.start()

    def _build_ui(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        main = QtWidgets.QVBoxLayout(root)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(6)

        toolbar = QtWidgets.QHBoxLayout()
        self.live_light = QtWidgets.QLabel("●")
        self.live_light.setStyleSheet(
            "color: #35d07f; font-size: 22px; font-weight: 700;"
        )
        self.status_label = QtWidgets.QLabel("Starting…")
        self.status_label.setStyleSheet("color: #a8b4c5;")

        self.down_btn = QtWidgets.QPushButton("▼ Tilt")
        self.up_btn = QtWidgets.QPushButton("▲ Tilt")
        self.past_btn = QtWidgets.QPushButton("◀ Past")
        self.future_btn = QtWidgets.QPushButton("Future ▶")
        self.live_btn = QtWidgets.QPushButton("LIVE")
        self.pan_btn = QtWidgets.QPushButton("Pan")
        self.zoom_btn = QtWidgets.QPushButton("Zoom Box")
        self.pan_btn.setCheckable(True)
        self.zoom_btn.setCheckable(True)
        self.pan_btn.setChecked(True)
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.pan_btn)
        self.mode_group.addButton(self.zoom_btn)
        self.wall_btn = QtWidgets.QPushButton("16 Panel")
        self.reset_btn = QtWidgets.QPushButton("Reset View")

        self.elev_combo = QtWidgets.QComboBox()
        self.time_label = QtWidgets.QLabel("—")
        self.time_label.setMinimumWidth(180)
        self.strategy_label = QtWidgets.QLabel("VCP ?")
        self.strategy_label.setStyleSheet(
            "color:#d8e2ef; font-weight:700; padding:5px;"
        )
        self.prediction_label = QtWidgets.QLabel("")
        self.prediction_label.setStyleSheet(
            "color:#ffb547; font-weight:700; padding:5px;"
        )
        self.time_label.setStyleSheet(
            "color: white; font-weight: 700; padding: 5px;"
        )

        for widget in (
            self.live_light,
            self.status_label,
            self.down_btn,
            self.elev_combo,
            self.up_btn,
            self.past_btn,
            self.time_label,
            self.future_btn,
            self.live_btn,
            self.pan_btn,
            self.zoom_btn,
            self.wall_btn,
            self.reset_btn,
        ):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
        toolbar.addWidget(self.strategy_label)
        toolbar.addWidget(self.prediction_label)
        main.addLayout(toolbar)

        self.stack = QtWidgets.QStackedWidget()
        main.addWidget(self.stack, 1)

        self.single = RadarCanvas(compact=False)
        self.stack.addWidget(self.single)

        wall_widget = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(wall_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(3)
        self.wall_canvases: list[RadarCanvas] = []

        for idx in range(16):
            canvas = RadarCanvas(compact=True)
            row, col = divmod(idx, 4)
            grid.addWidget(canvas, row, col)
            self.wall_canvases.append(canvas)
            canvas.activated.connect(self._activate_canvas)
            canvas.range_changed.connect(self._view_changed)

        self.stack.addWidget(wall_widget)

        self.single.activated.connect(self._activate_canvas)
        self.single.range_changed.connect(self._view_changed)

        self.down_btn.clicked.connect(lambda: self._step_elevation(-1))
        self.up_btn.clicked.connect(lambda: self._step_elevation(1))
        self.past_btn.clicked.connect(lambda: self._step_time(1))
        self.future_btn.clicked.connect(lambda: self._step_time(-1))
        self.live_btn.clicked.connect(self._go_live)
        self.pan_btn.clicked.connect(lambda: self._set_interaction_mode("pan"))
        self.zoom_btn.clicked.connect(lambda: self._set_interaction_mode("zoom"))
        self.wall_btn.clicked.connect(self._toggle_wall)
        self.reset_btn.clicked.connect(self._reset_view)
        self.elev_combo.currentIndexChanged.connect(self._combo_changed)

        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Up), self).activated.connect(
            self._keyboard_up
        )
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Down), self).activated.connect(
            self._keyboard_down
        )
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_PageUp), self).activated.connect(
            lambda: self._step_elevation(1)
        )
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_PageDown), self).activated.connect(
            lambda: self._step_elevation(-1)
        )
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Left), self).activated.connect(
            lambda: self._step_time(1)
        )
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Right), self).activated.connect(
            lambda: self._step_time(-1)
        )
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Home), self).activated.connect(
            self._go_live
        )

        self.single.reset_view()
        for canvas in self.wall_canvases:
            canvas.reset_view()

        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #070a10; color: #f1f5f9; }
            QPushButton, QComboBox {
                background: #111927;
                border: 1px solid #293548;
                border-radius: 6px;
                padding: 7px 10px;
                color: #f1f5f9;
            }
            QPushButton:hover { border-color: #4ea1ff; }
            QPushButton:checked {
                border-color: #4ea1ff;
                background: #17345a;
                color: white;
            }
            """
        )

    def closeEvent(self, event):
        self.worker.stop()
        self.worker.wait(2500)
        self.render_pool.clear()
        self.render_pool.waitForDone(1500)
        super().closeEvent(event)

    def _set_status(self, text: str):
        self.status_label.setText(text)

    def _apply_boundaries(self, data):
        self.boundary_data = data
        self.single.set_boundaries(data)
        for canvas in self.wall_canvases:
            canvas.set_boundaries(data)


    def _apply_snapshot(self, snapshot: dict[str, Any]):
        previous_anchor_id = self._anchor_scan().scan_id if self._anchor_scan() else None

        self.histories = snapshot["histories"]
        self.live_sequence = snapshot["live_sequence"]
        self.live_volume = snapshot["live_volume"]
        self.strategy = snapshot.get("strategy", self.strategy)
        self.scanning_scan = snapshot.get("scanning_scan")
        self.live_complete = bool(snapshot.get("live_complete", False))

        elevations = sorted(self.histories)
        if not elevations:
            return

        if self.anchor_elevation is None:
            self.anchor_elevation = elevations[0]
        else:
            self.anchor_elevation = min(
                elevations,
                key=lambda value: abs(value - self.anchor_elevation),
            )

        history = self._history()
        if self.follow_live:
            self.anchor_index = 0
        elif previous_anchor_id:
            match = next(
                (
                    idx
                    for idx, scan in enumerate(history)
                    if scan.scan_id == previous_anchor_id
                ),
                None,
            )
            if match is not None:
                self.anchor_index = match
            else:
                self.anchor_index = min(self.anchor_index, max(0, len(history) - 1))
        else:
            self.anchor_index = 0

        self._refresh_combo()
        self._resolve_wall()
        self._render_all()

    def _refresh_combo(self):
        self.elev_combo.blockSignals(True)
        self.elev_combo.clear()
        elevations = sorted(self.histories)
        for elevation in elevations:
            self.elev_combo.addItem(f"{elevation:.2f}°", elevation)

        if self.anchor_elevation is not None and elevations:
            index = min(
                range(len(elevations)),
                key=lambda idx: abs(elevations[idx] - self.anchor_elevation),
            )
            self.elev_combo.setCurrentIndex(index)
        self.elev_combo.blockSignals(False)

    def _history(self) -> list[RadarScan]:
        if self.anchor_elevation is None:
            return []
        return self.histories.get(self.anchor_elevation, [])

    def _anchor_scan(self) -> RadarScan | None:
        history = self._history()
        if not history:
            return None
        self.anchor_index = max(0, min(self.anchor_index, len(history) - 1))
        return history[self.anchor_index]

    def _resolve_wall(self):
        anchor = self._anchor_scan()
        self.wall_scans = {}

        if anchor is None:
            return

        # LIVE mode is mosaic-like: every elevation shows its own newest
        # available scan, even if that scan is several minutes older than the
        # currently active elevation. Once the user steps into history, the
        # highlighted/active scan becomes the common temporal ceiling.
        if self.follow_live:
            for elevation, history in self.histories.items():
                self.wall_scans[elevation] = history[0] if history else None
            return

        anchor_time = anchor.scan_time
        for elevation, history in self.histories.items():
            chosen = next(
                (
                    scan
                    for scan in history
                    if scan.scan_time <= anchor_time
                ),
                None,
            )
            if chosen is None and history:
                chosen = history[-1]
            self.wall_scans[elevation] = chosen

    def _render_all(self):
        anchor = self._anchor_scan()
        anchor_history = self._history()
        anchor_latest = bool(
            anchor is not None
            and anchor_history
            and anchor.scan_id == anchor_history[0].scan_id
        )
        anchor_scanning = bool(
            anchor is not None
            and self.scanning_scan is not None
            and abs(anchor.elevation - self.scanning_scan.elevation) <= 0.07
        )
        self.single.set_scan(
            anchor,
            self.live_volume,
            is_latest=anchor_latest,
            is_scanning=anchor_scanning,
        )
        self.single.set_active(True)

        elevations = sorted(self.histories)[:16]
        for idx, canvas in enumerate(self.wall_canvases):
            if idx < len(elevations):
                elevation = elevations[idx]
                scan = self.wall_scans.get(elevation)
                history = self.histories.get(elevation, [])
                is_latest = bool(
                    scan is not None
                    and history
                    and scan.scan_id == history[0].scan_id
                )
                is_scanning = bool(
                    self.scanning_scan is not None
                    and abs(elevation - self.scanning_scan.elevation) <= 0.07
                )
                canvas.set_scan(
                    scan,
                    self.live_volume,
                    is_latest=is_latest,
                    is_scanning=is_scanning,
                )
                canvas.set_active(
                    self.anchor_elevation is not None
                    and abs(elevation - self.anchor_elevation) <= 0.07
                )
                canvas.setProperty("elevation", elevation)
            else:
                canvas.set_scan(None, self.live_volume)
                canvas.set_active(False)
                canvas.setProperty("elevation", None)

        self._update_strategy_labels()
        self._update_time_label()
        self._refresh_age_labels()
        self.render_timer.start()

    def _set_interaction_mode(self, mode: str):
        self.interaction_mode = mode
        self.pan_btn.setChecked(mode == "pan")
        self.zoom_btn.setChecked(mode == "zoom")
        for canvas in [self.single, *self.wall_canvases]:
            canvas.set_interaction_mode(mode)

    def _refresh_age_labels(self):
        self.single.update_age_badge(self.live_volume)
        for canvas in self.wall_canvases:
            canvas.update_age_badge(self.live_volume)

    def _update_strategy_labels(self):
        scan_name = self.strategy.get("scan_name", "VCP ?")
        dynamic = self.strategy.get("dynamic_scan_type", "standard")
        self.strategy_label.setText(
            f"{scan_name} · {dynamic}"
            if dynamic and dynamic != "standard"
            else str(scan_name)
        )

        if self.scanning_scan is not None and not self.live_complete:
            self.prediction_label.setText(
                f"SCANNING… {self.scanning_scan.label}"
            )
        else:
            self.prediction_label.setText("")


    def _update_time_label(self):
        scan = self._anchor_scan()
        if scan is None:
            self.time_label.setText("—")
            return

        source = (
            "LIVE"
            if self.follow_live
            else (
                scan.kind
                if scan.kind != "BASE"
                else "HISTORY"
            )
        )
        self.time_label.setText(
            f"{scan.scan_time:%H:%M:%SZ} · {source} · {scan.elevation:.2f}°"
        )
        self.past_btn.setEnabled(self.anchor_index < len(self._history()) - 1)
        self.future_btn.setEnabled(self.anchor_index > 0)

    def _rerender_visible(self):
        # A fast interaction can create several obsolete queued viewports.
        # Drop queued (not-yet-running) work and schedule only the newest view.
        self.render_pool.clear()
        for canvas in [self.single, *self.wall_canvases]:
            canvas.cancel_pending_render()

        if not self._wall_mode:
            self.single.request_rerender(self.render_pool, priority=10)
            return

        for canvas in self.wall_canvases:
            if canvas.scan is None:
                continue
            elevation = canvas.property("elevation")
            active = (
                elevation is not None
                and self.anchor_elevation is not None
                and abs(float(elevation) - self.anchor_elevation) <= 0.07
            )
            canvas.request_rerender(
                self.render_pool,
                priority=10 if active else 0,
            )

    def _view_changed(self, source: RadarCanvas):
        if self._syncing_ranges or not hasattr(self, "render_timer"):
            return
        x_range, y_range = source.visible_ranges()
        self._syncing_ranges = True
        try:
            for canvas in [self.single, *self.wall_canvases]:
                if canvas is source:
                    continue
                canvas.set_view_ranges(x_range, y_range)
        finally:
            self._syncing_ranges = False
        self.render_timer.start()

    def _activate_canvas(self, canvas: RadarCanvas):
        elevation = canvas.property("elevation")
        if elevation is None:
            return
        self.anchor_elevation = float(elevation)

        displayed = canvas.scan
        history = self._history()
        if displayed is not None:
            found = next(
                (
                    idx
                    for idx, scan in enumerate(history)
                    if scan.scan_id == displayed.scan_id
                ),
                0,
            )
            self.anchor_index = found
            self.follow_live = found == 0
        else:
            self.anchor_index = 0
            self.follow_live = True

        self._refresh_combo()
        self._resolve_wall()
        self._render_all()

    def _combo_changed(self, index: int):
        if index < 0:
            return
        elevation = self.elev_combo.itemData(index)
        if elevation is None:
            return

        old_anchor = self._anchor_scan()
        old_time = old_anchor.scan_time if old_anchor else datetime.now(timezone.utc)
        self.anchor_elevation = float(elevation)

        history = self._history()
        if history:
            if self.follow_live:
                self.anchor_index = 0
            else:
                self.anchor_index = next(
                    (
                        idx
                        for idx, scan in enumerate(history)
                        if scan.scan_time <= old_time
                    ),
                    len(history) - 1,
                )
        else:
            self.anchor_index = 0

        self._resolve_wall()
        self._render_all()

    def _keyboard_up(self):
        if self._wall_mode:
            self._pan_vertical(1)
        else:
            self._step_elevation(1)

    def _keyboard_down(self):
        if self._wall_mode:
            self._pan_vertical(-1)
        else:
            self._step_elevation(-1)


    def _pan_vertical(self, direction: int):
        x_range, y_range = self.single.visible_ranges()
        span = y_range[1] - y_range[0]
        delta = span * 0.14 * float(direction)
        new_y = (y_range[0] + delta, y_range[1] + delta)

        self._syncing_ranges = True
        try:
            for canvas in [self.single, *self.wall_canvases]:
                canvas.set_view_ranges(x_range, new_y)
        finally:
            self._syncing_ranges = False
        self.render_timer.start()


    def _step_elevation(self, direction: int):
        elevations = sorted(self.histories)
        if not elevations or self.anchor_elevation is None:
            return

        current = min(
            range(len(elevations)),
            key=lambda idx: abs(elevations[idx] - self.anchor_elevation),
        )
        target = current + direction
        if target < 0 or target >= len(elevations):
            return

        old_anchor = self._anchor_scan()
        old_time = old_anchor.scan_time if old_anchor else datetime.now(timezone.utc)

        self.anchor_elevation = elevations[target]
        history = self._history()
        if self.follow_live:
            self.anchor_index = 0
        else:
            self.anchor_index = next(
                (
                    idx
                    for idx, scan in enumerate(history)
                    if scan.scan_time <= old_time
                ),
                max(0, len(history) - 1),
            )
        self._refresh_combo()
        self._resolve_wall()
        self._render_all()

    def _step_time(self, direction: int):
        history = self._history()
        if not history:
            return
        target = self.anchor_index + direction
        if target < 0 or target >= len(history):
            return
        self.follow_live = False
        self.anchor_index = target
        self._resolve_wall()
        self._render_all()

    def _go_live(self):
        self.follow_live = True
        self.anchor_index = 0
        self._resolve_wall()
        self._render_all()

    def _toggle_wall(self):
        self._wall_mode = not self._wall_mode
        self.stack.setCurrentIndex(1 if self._wall_mode else 0)
        self.wall_btn.setText("Single Panel" if self._wall_mode else "16 Panel")
        self.render_timer.start()

    def _reset_view(self):
        x_range = (-DEFAULT_RANGE_KM, DEFAULT_RANGE_KM)
        y_range = (-DEFAULT_RANGE_KM, DEFAULT_RANGE_KM)
        self._syncing_ranges = True
        try:
            for canvas in [self.single, *self.wall_canvases]:
                canvas.set_view_ranges(x_range, y_range)
        finally:
            self._syncing_ranges = False
        self.render_timer.start()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("KMOB Native Radar Analyst")
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()

# Native KMOB radar desktop prototype\nfrom __future__ import annotations

import math
import os
import re
import sys
import tempfile
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import numpy as np
import xradar as xd
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
    if ranges.size < 2:
        return np.full((height, width), np.nan, dtype=np.float32)

    gate = np.searchsorted(ranges, rr, side="left")
    gate = np.clip(gate, 0, ranges.size - 1)

    lower_gate = np.maximum(gate - 1, 0)
    choose_lower = np.abs(rr - ranges[lower_gate]) < np.abs(rr - ranges[gate])
    gate = np.where(choose_lower, lower_gate, gate)

    cached_sort = _SCAN_SORT_CACHE.get(scan.scan_id)
    if cached_sort is None:
        order = np.argsort(scan.azimuth)
        az_sorted = np.ascontiguousarray(scan.azimuth[order])
        _SCAN_SORT_CACHE[scan.scan_id] = (order, az_sorted)
    else:
        order, az_sorted = cached_sort
    data_sorted = scan.reflectivity[order]

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

    sampled = data_sorted[ray_sorted, gate]
    sampled = np.asarray(sampled, dtype=np.float32)
    sampled[rr > ranges[-1]] = np.nan
    return np.ascontiguousarray(sampled)


class RadarDataWorker(QtCore.QThread):
    snapshot = QtCore.Signal(object)
    status = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self._running = True
        self.histories: dict[float, list[RadarScan]] = defaultdict(list)
        self.current_live_volume: str | None = None
        self.live_sequence: list[RadarScan] = []
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

    def _candidate_archive_prefixes(self) -> list[str]:
        now = datetime.now(timezone.utc)
        return [
            f"{now:%Y/%m/%d}/{RADAR_ID}/",
            f"{(now - timedelta(days=1)):%Y/%m/%d}/{RADAR_ID}/",
        ]

    def _recent_archive_keys(self) -> list[str]:
        found: list[tuple[datetime, str]] = []
        for prefix in self._candidate_archive_prefixes():
            paginator = self.archive_s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=ARCHIVE_BUCKET,
                Prefix=prefix,
            ):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    name = key.rsplit("/", 1)[-1]
                    if "_MDM" in name or not name.startswith(RADAR_ID):
                        continue
                    if "_V06" not in name and not name.endswith(".gz"):
                        continue
                    found.append((obj["LastModified"], key))

        found.sort(reverse=True)
        return [key for _, key in found[:ARCHIVE_VOLUMES]]

    def _archive_path(self, key: str) -> Path:
        return CACHE_DIR / key.rsplit("/", 1)[-1]

    def _bootstrap_archive(self):
        keys = self._recent_archive_keys()
        if not keys:
            self.status.emit("No recent completed KMOB volume found.")
            return

        for idx, key in enumerate(reversed(keys)):
            if not self._running:
                return
            path = self._archive_path(key)
            if not path.exists():
                self.status.emit(f"Downloading archive volume {idx + 1}/{len(keys)}…")
                self.archive_s3.download_file(
                    ARCHIVE_BUCKET,
                    key,
                    str(path),
                )

            tree = xd.io.open_nexradlevel2_datatree(
                str(path),
                incomplete_sweep="drop",
            )
            scans = _extract_scans(tree, key, "archive")
            self._merge_scans(scans)

        self.status.emit("Archive ready · connecting live chunks…")
        self._emit_snapshot()

    def _latest_chunk_prefix(self) -> str | None:
        root = f"{RADAR_ID}/"
        response = self.chunk_s3.list_objects_v2(
            Bucket=CHUNK_BUCKET,
            Prefix=root,
            Delimiter="/",
            MaxKeys=1000,
        )
        prefixes = [item["Prefix"] for item in response.get("CommonPrefixes", [])]
        if not prefixes:
            return None

        def prefix_key(prefix: str):
            leaf = prefix.rstrip("/").rsplit("/", 1)[-1]
            try:
                return int(leaf), prefix
            except ValueError:
                return -1, prefix

        return max(prefixes, key=prefix_key)

    def _chunk_objects(self, prefix: str) -> list[dict[str, Any]]:
        response = self.chunk_s3.list_objects_v2(
            Bucket=CHUNK_BUCKET,
            Prefix=prefix,
            MaxKeys=1000,
        )
        objects = response.get("Contents", [])

        def order(item):
            name = item["Key"].rsplit("/", 1)[-1]
            match = re.search(r"(\d+)-(?:S|I|E)$", name)
            return int(match.group(1)) if match else 999999

        return sorted(objects, key=order)

    def _refresh_live(self) -> bool:
        prefix = self._latest_chunk_prefix()
        if not prefix:
            return False

        if prefix != self.current_live_volume:
            self.current_live_volume = prefix
            self._chunk_bytes = {}
            self.live_sequence = []
            self.status.emit(f"LIVE · new volume {prefix.rstrip('/').rsplit('/', 1)[-1]}")

        objects = self._chunk_objects(prefix)
        if not objects:
            return False

        changed = False
        for obj in objects:
            key = obj["Key"]
            if key in self._chunk_bytes:
                continue
            self._chunk_bytes[key] = self.chunk_s3.get_object(
                Bucket=CHUNK_BUCKET,
                Key=key,
            )["Body"].read()
            changed = True

        if not changed:
            return False

        ordered_keys = [obj["Key"] for obj in objects if obj["Key"] in self._chunk_bytes]
        chunks = [self._chunk_bytes[key] for key in ordered_keys]
        tree = xd.io.open_nexradlevel2_datatree(
            chunks,
            incomplete_sweep="pad",
        )
        scans = _extract_scans(tree, prefix, "live")
        self.live_sequence = scans
        self._merge_scans(scans)

        last = scans[-1] if scans else None
        if last:
            self.status.emit(
                f"LIVE · {last.label} · {last.completion:.0f}% · {len(chunks)} chunks"
            )
        return True

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
        self.snapshot.emit(
            {
                "histories": copied,
                "live_sequence": list(self.live_sequence),
                "live_volume": self.current_live_volume,
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
        self.setMouseMode(pg.ViewBox.RectMode)
        self.setLimits(
            xMin=-500,
            xMax=500,
            yMin=-500,
            yMax=500,
            minXRange=1.0,
            minYRange=1.0,
        )

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.activated.emit()
        super().mouseClickEvent(ev)

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() == QtCore.Qt.MouseButton.RightButton:
            ev.accept()
            delta = ev.pos() - ev.lastPos()
            delta = self.mapSceneToView(ev.scenePos()) - self.mapSceneToView(
                ev.scenePos() - delta
            )
            self.translateBy(x=-delta.x(), y=-delta.y())
            return
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
        self._rerendering = False

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
            "QLabel { background: rgba(4,8,13,190); color: #9fb0c5; "
            "padding: 3px 5px; border-radius: 4px; }"
        )

        overlay = QtWidgets.QGridLayout()
        overlay.setContentsMargins(6, 6, 6, 6)
        overlay.addWidget(self.title, 0, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        overlay.addWidget(self.badge, 0, 1, QtCore.Qt.AlignmentFlag.AlignRight)
        overlay.setRowStretch(1, 1)

        stack = QtWidgets.QStackedLayout(self)
        stack.setStackingMode(QtWidgets.QStackedLayout.StackingMode.StackAll)
        stack.addWidget(self.plot)

        overlay_widget = QtWidgets.QWidget()
        overlay_widget.setLayout(overlay)
        overlay_widget.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        stack.addWidget(overlay_widget)

        self.view_box.activated.connect(lambda: self.activated.emit(self))
        self.view_box.sigRangeChanged.connect(
            lambda *_: self.range_changed.emit(self)
        )

    def set_scan(self, scan: RadarScan | None, current_live_volume: str | None):
        self.scan = scan
        if scan is None:
            self.title.setText("—")
            self.badge.setText("NO SCAN")
            self.image.clear()
            return

        self.title.setText(f"{scan.elevation:.2f}°")
        if scan.source == "live" and scan.volume_id == current_live_volume:
            if scan.kind == "BASE":
                age = "LIVE"
            else:
                age = f"{scan.kind} #{scan.sequence_number}"
        else:
            age = scan.scan_time.strftime("%H:%M:%SZ")
        self.badge.setText(age)

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

    def rerender(self):
        if self.scan is None:
            return

        x_range, y_range = self.visible_ranges()
        size = self.plot.viewport().size()
        if self.compact:
            width = max(150, min(size.width(), 320))
            height = max(150, min(size.height(), 320))
        else:
            width = max(350, min(size.width(), 1000))
            height = max(350, min(size.height(), 1000))

        image = render_scan(
            self.scan,
            x_range,
            y_range,
            width,
            height,
        )
        rect = QtCore.QRectF(
            x_range[0],
            y_range[0],
            x_range[1] - x_range[0],
            y_range[1] - y_range[0],
        )
        self.image.setImage(
            image,
            autoLevels=False,
            levels=(-10, 80),
        )
        self.image.setRect(rect)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KMOB Native Radar Analyst")
        self.resize(1500, 980)

        self.histories: dict[float, list[RadarScan]] = {}
        self.live_sequence: list[RadarScan] = []
        self.live_volume: str | None = None

        self.anchor_elevation: float | None = None
        self.anchor_index = 0
        self.wall_scans: dict[float, RadarScan | None] = {}
        self.follow_live = True

        self._syncing_ranges = False
        self._wall_mode = False

        self._build_ui()

        self.render_timer = QtCore.QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(100)
        self.render_timer.timeout.connect(self._rerender_visible)

        self.worker = RadarDataWorker()
        self.worker.snapshot.connect(self._apply_snapshot)
        self.worker.status.connect(self._set_status)
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
        self.wall_btn = QtWidgets.QPushButton("16 Panel")
        self.reset_btn = QtWidgets.QPushButton("Reset View")

        self.elev_combo = QtWidgets.QComboBox()
        self.time_label = QtWidgets.QLabel("—")
        self.time_label.setMinimumWidth(180)
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
            self.wall_btn,
            self.reset_btn,
        ):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
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
        self.wall_btn.clicked.connect(self._toggle_wall)
        self.reset_btn.clicked.connect(self._reset_view)
        self.elev_combo.currentIndexChanged.connect(self._combo_changed)

        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Up), self).activated.connect(
            lambda: self._step_elevation(1)
        )
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Down), self).activated.connect(
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
            """
        )

    def closeEvent(self, event):
        self.worker.stop()
        self.worker.wait(2500)
        super().closeEvent(event)

    def _set_status(self, text: str):
        self.status_label.setText(text)

    def _apply_snapshot(self, snapshot: dict[str, Any]):
        previous_anchor_id = self._anchor_scan().scan_id if self._anchor_scan() else None

        self.histories = snapshot["histories"]
        self.live_sequence = snapshot["live_sequence"]
        self.live_volume = snapshot["live_volume"]

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
        self.single.set_scan(anchor, self.live_volume)
        self.single.set_active(True)

        elevations = sorted(self.histories)[:16]
        for idx, canvas in enumerate(self.wall_canvases):
            if idx < len(elevations):
                elevation = elevations[idx]
                scan = self.wall_scans.get(elevation)
                canvas.set_scan(scan, self.live_volume)
                canvas.set_active(
                    self.anchor_elevation is not None
                    and abs(elevation - self.anchor_elevation) <= 0.07
                )
                canvas.setProperty("elevation", elevation)
            else:
                canvas.set_scan(None, self.live_volume)
                canvas.set_active(False)
                canvas.setProperty("elevation", None)

        self._update_time_label()
        self.render_timer.start()

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
        self.single.rerender()
        if self._wall_mode:
            for canvas in self.wall_canvases:
                if canvas.scan is not None:
                    canvas.rerender()

    def _view_changed(self, source: RadarCanvas):
        if self._syncing_ranges:
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

# KMOB Native Radar Analyst Prototype

Native Windows radar workstation prototype built with PySide6 + PyQtGraph.

## Why this branch exists

The browser prototype proved the Level-II ingest and scan-history concepts, but the HTTP/PNG rendering path adds latency that is undesirable for storm interrogation. This branch keeps radar arrays in process and redraws directly into native Qt graphics.

## First native prototype

- KMOB NEXRAD Level-II reflectivity.
- Real-time AWS `unidata-nexrad-level2-chunks` ingestion with Xradar.
- Completed-volume bootstrap/fallback from `unidata-nexrad-level2`.
- SAILS/MRLE labels from Level-II metadata when available.
- Per-elevation scan history retained in memory.
- Single-panel and 16-panel views.
- Click a 16-panel pane to make it the temporal anchor.
- Left/Right arrows step the selected elevation's actual scan history.
- Other panes resolve to the newest scan that existed at or before the anchor time.
- Left-drag draws a zoom box.
- Right-drag pans.
- Mouse wheel zooms.
- Any viewport change is synchronized across all panes.
- After zoom/pan settles, raw polar Level-II data is re-interpolated into the visible viewport rather than merely scaling an old PNG.
- No FastAPI, browser, PNG encoding, or HTTP requests in the display path.

## Install on Windows

From the repository:

```powershell
conda env create -f desktop_environment.yml
conda activate kmob-radar-desktop
```

Or install the desktop packages into the existing `kmob-radar` environment. On Windows, prefer Conda-forge for the complete Qt DLL stack:

```powershell
conda activate kmob-radar
python -m pip uninstall -y PySide6 PySide6_Addons PySide6_Essentials shiboken6 pyqtgraph
conda install -n kmob-radar --solver=libmamba --override-channels -c conda-forge "pyside6>=6.10,<6.12" "pyqtgraph=0.14" numba -y
```

Verify Qt before launching:

```powershell
python -c "from PySide6 import QtCore, QtGui, QtWidgets; print('PySide6 OK:', QtCore.qVersion())"
python -c "import PySide6; import pyqtgraph as pg; print('GUI stack OK:', pg.__version__)"
```

Xradar 0.12+ is required for direct NEXRAD chunk ingestion.

## Run

```powershell
python desktop_radar.py
```

The window opens immediately. Archive and live data load on a worker thread.

## Controls

| Control | Action |
|---|---|
| Left-drag | Draw zoom rectangle and rerender |
| Right-drag | Pan and rerender |
| Mouse wheel | Zoom and rerender |
| Double-click | Reset to configured radar range |
| Up / Down | Higher / lower base elevation |
| Left / Right | Older / newer scan for active elevation |
| Home | Return active elevation to newest scan |
| 16 Panel | Toggle synchronized 4x4 elevation wall |
| Click wall pane | Make pane/elevation the time anchor |

## Current rendering path

The first desktop build still performs polar-to-Cartesian interpolation with NumPy, but it does so directly in memory and only for the visible viewport. PyQtGraph receives C-contiguous float32 arrays and a cached 256-entry uint8 reflectivity LUT.

A later phase can replace the NumPy interpolator with a custom OpenGL polar texture shader without changing the ingest/history model.

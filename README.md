# KMOB Level-II Volume Explorer — Prototype

Interactive NEXRAD Level-II interrogation prototype for **KMOB**.

## Current rapid-interrogation build

- Polls the public AWS `unidata-nexrad-level2` completed-volume bucket every **5 seconds** for a newer KMOB volume.
- Limits S3 discovery to the most recent hours instead of repeatedly listing an entire day.
- Uses a fast NumPy/Pillow polar rasterizer instead of rebuilding every PPI through Matplotlib.
- Caches rendered sweeps and preloads the immediately adjacent tilts.
- Supports rapid tilt stepping:
  - **Up Arrow** = next higher tilt
  - **Down Arrow** = next lower tilt
  - Mouse wheel **up** over the radar = higher tilt
  - Mouse wheel **down** over the radar = lower tilt
  - Click a row in the Vertical Inspector to jump to that tilt
- Large on-radar HUD displays:
  - current elevation angle
  - tilt number
  - cursor azimuth/range
  - approximate beam-center height in kft ARL
- Clicking a point locks an all-tilt vertical inspection and uses Py-ART gate geometry for the exact beam-center height at that location.
- Optional **2-D Smooth** mode performs bilinear interpolation between neighboring azimuth/range samples in the displayed sweep.
- New volumes preserve the nearest current elevation and automatically refresh a locked interrogation point.

## Radar moments

- Reflectivity
- Velocity
- ZDR
- Correlation Coefficient
- Differential Phase
- Spectrum Width

## Windows setup

The recommended environment is Conda/Miniconda with Python 3.11.

```powershell
conda create -n kmob-radar --solver=libmamba --override-channels -c conda-forge python=3.11 arm_pyart pip -y
conda activate kmob-radar
python -m pip install fastapi "uvicorn[standard]" boto3
```

Then launch from the project directory:

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open:

```
http://127.0.0.1:8000
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RADAR_ID` | `KMOB` | Radar ICAO ID |
| `NEXRAD_BUCKET` | `unidata-nexrad-level2` | Completed-volume AWS bucket |
| `POLL_SECONDS` | `5` | Frequency to check for a newer completed volume |
| `DEFAULT_RANGE_KM` | `150` | Initial display radius |
| `RADAR_RASTER_SIZE` | `640` | PPI raster dimensions; lower is faster |
| `RADAR_CACHE_DIR` | OS temp directory | Download cache |

## Current API

- `GET /api/status` — ingest/freshness status
- `POST /api/refresh` — force an AWS check
- `GET /api/volume` — current volume metadata, fields, and sweeps
- `GET /api/image/{field}/{sweep}.png?range_km=150&smooth=true` — cached/interpolated PPI
- `GET /api/inspect?x_km=...&y_km=...` — all-tilt vertical inspection

## Important latency distinction

This build is now much faster for **interrogating the volume already loaded**, but it still reads the AWS **completed-volume** Level-II source. A 5-second poll cannot make a not-yet-completed volume appear early.

The next ingest phase is the real-time `unidata-nexrad-level2-chunks` source using Xradar's streaming NEXRAD reader. That source can expose incomplete/current sweeps while the radar is still collecting the volume, which is the path to true tilt-as-it-arrives operation.

## Next development targets

1. Xradar Level-II chunk ingestion with incomplete sweeps padded in real time.
2. Current-scan progress display showing which tilt is actively arriving.
3. A→B vertical cross-sections.
4. Time-height history for a locked storm point.
5. Derived echo-top/core-height products.
6. ZDR/KDP/CC column analysis.
7. Rotation-depth / azimuthal-shear analysis.
8. Storm-centered 3-D volumes.

# KMOB Level-II Volume Explorer — Prototype

Interactive NEXRAD Level-II interrogation prototype for **KMOB**, including true tilt-as-it-arrives streaming.

## Current build

### Live Level-II ingest
- Uses the AWS `unidata-nexrad-level2-chunks` real-time bucket when Xradar 0.12+ is installed.
- Xradar opens partial volumes with `incomplete_sweep="pad"`, allowing the current elevation to appear before the full 360° sweep or volume is finished.
- Falls back to the completed `unidata-nexrad-level2` archive if the chunk stream is temporarily unavailable.
- A blinking **LIVE** light is shown only when viewing frame 0 from the chunk stream.

### Base tilts only
Supplemental/repeat elevations are suppressed in the user-facing tilt list:
- SAILS repeats are not shown as additional 0.18°/lowest-elevation tilts.
- MRLE repeated low elevations are also excluded.
- Consecutive same-elevation split cuts are grouped into one logical tilt so the viewer can still select the appropriate radar moment without displaying duplicate elevations.

### Interrogation controls
- Mouse wheel over the radar: step vertically through tilts.
- **Up / Down Arrow**: higher/lower tilt.
- **Left / Right Arrow**: older/newer volume.
- Rolling archive history of about **10 completed volumes**.
- LIVE button jumps back to the newest chunk-stream frame.
- Click a radar point for an all-tilt vertical inspector.
- Beam-center height shown in kft ARL.
- Optional 2-D polar interpolation.

### 16-panel tilt wall
- Toggle between single-panel interrogation and a **4×4 / 16-panel** display.
- Displays up to the first 16 unique base elevations.
- Each panel updates when new live chunks arrive.
- The currently incomplete scan shows its approximate receive percentage.
- Clicking a panel jumps back to single-panel interrogation at that elevation.

### Map reference
- State and county outlines are downloaded once from the U.S. Census Bureau 2025 5m Cartographic Boundary files and cached locally.
- Boundaries are projected into radar-relative coordinates and stay aligned with the PPI at each display range.

## Windows setup / update

Activate the existing environment:

```powershell
conda activate kmob-radar
```

Install/upgrade Xradar for true streaming support:

```powershell
conda install -n kmob-radar --solver=libmamba --override-channels -c conda-forge "xradar>=0.12,<0.13" -y
```

Then update the branch:

```powershell
git checkout kmob-level2-prototype
git pull origin kmob-level2-prototype
```

Launch:

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open:

```
http://127.0.0.1:8000
```

The first state/county overlay request may take a little longer because the Census boundary ZIPs are downloaded and extracted once. Subsequent runs use the local boundary cache.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RADAR_ID` | `KMOB` | Radar ICAO ID |
| `NEXRAD_BUCKET` | `unidata-nexrad-level2` | Completed-volume history bucket |
| `NEXRAD_CHUNK_BUCKET` | `unidata-nexrad-level2-chunks` | Real-time chunk bucket |
| `POLL_SECONDS` | `2` | Live chunk check interval |
| `HISTORY_FRAMES` | `10` | Completed volumes retained in the time navigator |
| `DEFAULT_RANGE_KM` | `150` | Initial display radius |
| `RADAR_RASTER_SIZE` | `640` | Main PPI raster size |
| `RADAR_CACHE_DIR` | OS temp directory | Level-II and boundary cache |

## Controls

| Control | Action |
|---|---|
| Mouse wheel | Step through tilts |
| ↑ / ↓ | Step through tilts |
| ← / → | Older / newer volume |
| LIVE | Return to real-time frame |
| 16 Panel | Toggle the all-tilt wall |
| Click PPI | Lock a vertical interrogation point |

## Data sources

- NOAA/NEXRAD Level-II via AWS Open Data.
- Real-time Level-II chunk stream via `unidata-nexrad-level2-chunks`.
- U.S. Census Bureau generalized state and county boundaries.

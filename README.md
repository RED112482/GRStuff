# KMOB Level-II Volume Explorer — Prototype

A first-pass interactive NEXRAD Level-II viewer/analysis prototype for **KMOB**.

## What this MVP does

- Polls the public AWS `unidata-nexrad-level2` bucket for the newest completed KMOB Level-II volume.
- Downloads and decodes the newest volume with Py-ART.
- Displays selectable radar moments:
  - Reflectivity
  - Velocity
  - ZDR
  - Correlation Coefficient
  - Differential Phase
  - Spectrum Width
- Lets you switch through all available elevation scans.
- Click anywhere in the radar display to inspect that horizontal location through **every elevation angle**.
- Builds a vertical table showing height and radar-moment values for each tilt.
- Automatically checks AWS for a newer volume every 30 seconds.

## Important prototype limitation

This first version uses the completed-volume Level-II bucket, not the Level-II **chunk** bucket. That is intentional so the ingest/analysis/display workflow can be validated first. The backend is separated from the UI so the next phase can replace the completed-volume poller with the chunk stream and update the analysis as individual elevations arrive.

## Run locally

Python 3.11 is recommended.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Then open:

```
http://localhost:8000
```

No AWS credentials are required; the NOAA/Unidata NEXRAD bucket is public.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RADAR_ID` | `KMOB` | Radar ICAO ID |
| `NEXRAD_BUCKET` | `unidata-nexrad-level2` | AWS bucket |
| `POLL_SECONDS` | `30` | Frequency to check for new volumes |
| `DEFAULT_RANGE_KM` | `150` | Initial display radius |
| `RADAR_CACHE_DIR` | OS temp directory | Download cache |

## API

- `GET /api/status` — ingest status
- `POST /api/refresh` — force an AWS check
- `GET /api/volume` — volume metadata, fields, and sweeps
- `GET /api/image/{field}/{sweep}.png?range_km=150` — PPI image
- `GET /api/inspect?x_km=...&y_km=...` — all-tilt vertical inspection at a selected point

## Next development targets

1. Level-II chunk ingestion for tilt-by-tilt live updates.
2. Interactive A→B vertical cross-sections.
3. Time-height history for a selected point/storm.
4. Derived echo-top and core-height products.
5. ZDR/KDP/CC column analysis.
6. Storm-centered 3-D cubes and isosurfaces.
7. Rotation-depth / azimuthal-shear analysis.

## Hosting note

GitHub can store/version this application, but GitHub Pages cannot execute the Python Level-II backend. The backend must run locally or on a Python-capable host/container. The included Dockerfile provides a portable starting point.

# Geospatial File Measurement API

A FastAPI service that accepts a **Shapefile (`.zip`)** or **KML** file, extracts every feature
(geometry, CRS, attributes) and returns **area** for polygons and **length** for lines.
Measurements are always computed in a metric projected CRS, never on raw lat/lon degrees.

## Setup

Requires Python 3.11+ (developed on 3.12). No system GDAL/PROJ install is needed: `pyogrio`,
`shapely` and `pyproj` ship wheels that bundle them.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Interactive docs: <http://localhost:8000/docs>. Health check: `GET /health`.

**Docker**

```bash
docker build -t geo-measure-api .
docker run -p 8000:8000 -v geo-data:/data geo-measure-api
```

**Tests**

```bash
pytest -q
```

**Configuration** (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/app.db` | Any SQLAlchemy URL (e.g. Postgres) |
| `UPLOAD_DIR` | `./data/uploads` | Where original uploads are kept |
| `MAX_UPLOAD_MB` | `50` | Max upload size (checked while streaming) |
| `MAX_UNCOMPRESSED_MB` | `500` | Max size a zip may expand to |
| `MAX_FEATURES` | `100000` | Max features per file |

## API

Sample inputs live in `samples/` (`sample.kml`, `sample_parcels.zip`).

### `POST /api/files/` - upload and process

```bash
curl -F "file=@samples/sample.kml" http://localhost:8000/api/files/
```

`201 Created`
```json
{
  "id": "bd685b72a1f84ad58370df0f7118a739",
  "filename": "sample.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "crs_assumed": false,
  "status": "COMPLETED",
  "error": null,
  "created_at": "2026-10-07T11:38:18.608894Z"
}
```

| Code | When |
|---|---|
| 201 | File processed |
| 413 | Over `MAX_UPLOAD_MB` |
| 415 | Not a `.kml` or `.zip` |
| 422 | File is unusable (corrupt, no `.shp`, missing `.dbf`/`.shx`, unsafe zip paths, no features, no CRS and coordinates are not lon/lat). Body: `{"detail": {"id", "status": "FAILED", "error"}}`; the failed record stays queryable |

### `GET /api/files/{id}/` - file information

Same shape as the upload response. `status` is `PROCESSING`, `COMPLETED` or `FAILED`.
`crs` is `MIXED` if layers in one file use different CRSs (each feature still reports its own).

### `GET /api/files/{id}/features/?limit=100&offset=0` - extracted features

```json
{
  "file_id": "bd68...", "total": 3, "limit": 100, "offset": 0,
  "results": [{
    "index": 0, "layer": "Sample survey", "geometry_type": "Polygon",
    "crs": "EPSG:4326", "crs_assumed": false,
    "geometry": {"type": "Polygon", "coordinates": [[[77.59, 12.978, 0.0], "..."]]},
    "properties": {"Name": "Park parcel", "description": "Polygon: area expected"}
  }]
}
```

Geometry is GeoJSON in the file's **original** CRS.

### `GET /api/files/{id}/measurements/?limit=100&offset=0` - measurements

```json
{
  "file_id": "bd68...", "total": 3, "limit": 100, "offset": 0,
  "summary": {
    "total_features": 3, "ok": 2, "not_applicable": 1, "unsupported": 0,
    "skipped": 0, "error": 0, "total_area_m2": 648908.827, "total_length_m": 1001.794
  },
  "results": [{
    "index": 0, "layer": "Sample survey", "geometry_type": "Polygon", "crs": "EPSG:4326",
    "properties": {"Name": "Park parcel"},
    "measurement": {
      "status": "OK", "type": "area", "value": 648908.827, "unit": "m2",
      "geodesic_value": 648156.515, "projected_crs": "EPSG:32643", "notes": []
    }
  }]
}
```

`summary` covers the whole file regardless of pagination. Returns `409` if the file is not `COMPLETED`.

**Per-feature `measurement.status`**

| Status | Meaning |
|---|---|
| `OK` | Area (`m2`) for Polygon/MultiPolygon, length (`m`) for LineString/MultiLineString |
| `NOT_APPLICABLE` | Point/MultiPoint: no measurement required |
| `UNSUPPORTED` | Any other type (e.g. GeometryCollection): reported, never a crash |
| `SKIPPED` | Null or empty geometry |
| `ERROR` | Measuring this one feature failed; the rest of the file is unaffected |

## Architecture

```
app/
  main.py                app + lifespan (creates tables/folders)
  config.py              env-based settings
  database.py            SQLAlchemy engine/session
  models.py              UploadedFile, Feature
  schemas.py             Pydantic response models
  routers/files.py       HTTP layer only: validation, status codes, pagination
  services/ingest.py     safe upload saving, safe zip extraction, file -> GeoDataFrames
  services/measure.py    CRS resolution/selection + Measurer
  services/processor.py  orchestration: read -> measure -> persist
tests/                   unit tests (measurement) + end-to-end API tests
```

**File-processing flow**

1. The router checks the extension (415) and streams the upload to `UPLOAD_DIR/<id>/`, aborting at the size cap (413).
2. A `PROCESSING` record is committed.
3. `iter_layers` reads the file layer by layer: a KML yields one layer per Folder/Document; a zip is extracted
   safely and each `.shp` becomes a layer.
4. For each layer the CRS is resolved, and every feature is stored with its GeoJSON geometry, geometry type,
   CRS label, attributes and measurement.
5. Features and the final status are committed in **one transaction**. Any failure rolls back the features and
   marks the file `FAILED` with a client-safe message.

**Measurement flow** (`Measurer.measure`, per feature)

1. Classify: polygonal -> area, linear -> length, point -> not applicable, anything else -> unsupported.
2. Drop Z (KML carries altitude); repair invalid polygons with `make_valid` and note it.
3. Transform to WGS84 lon/lat if the source CRS is something else.
4. Pick a metric CRS from the feature centroid, project, and measure with Shapely.
5. Compute a geodesic (WGS84 ellipsoid) value with `pyproj.Geod` as a cross-check.
   A note is added if the two differ by more than 1%.
6. Any exception is caught and returned as `status: ERROR` for that feature only.

**CRS handling**

- The CRS is read from the file (`.prj` for shapefiles; KML is always WGS84) and reported per feature as `EPSG:xxxx`.
- Geometry is never measured in degrees. Projected sources (e.g. EPSG:32643) are also re-projected, so
  units like US feet can't leak into results.
- Metric CRS choice: the UTM zone of the feature's centroid (`326xx` north / `327xx` south);
  polar stereographic EPSG:3995 above 84°N and EPSG:3031 below 80°S, where UTM is undefined.
- **Missing CRS:** if a shapefile has no `.prj` and its coordinates fit within lon/lat bounds, WGS84 is assumed and
  `crs_assumed: true` is returned. If the coordinates are clearly projected, the upload is rejected rather than guessed.

## Design decisions and alternatives

| Decision | Why | Alternative considered |
|---|---|---|
| **FastAPI** | Typed request/response models and automatic OpenAPI docs with little code | Django + DRF: more batteries (admin, ORM) than this service needs |
| **Synchronous processing** inside `POST` | Files are small-to-medium, so processing is quick. Fewer moving parts and easy to test | Background worker (Celery/RQ) returning `PROCESSING`. The `status` field and 3-state model are already in place for it; see Future scope |
| **UTM zone per feature** | Accurate for features within a zone, works worldwide, and a file spanning several zones is still measured correctly feature by feature | One CRS per file (breaks for wide-area files); equal-area projections such as EPSG:6933 (exact area, but they distort length, so two CRSs would be needed) |
| **Geodesic cross-check returned alongside** | UTM is not equal-area. `geodesic_value` is the ellipsoidal reference, so clients can see the error instead of trusting blindly | Geodesic only (accurate, but the spec asks for projection) |
| **Features stored as rows with JSON columns** | Simple, portable (SQLite or Postgres), paginates easily with `LIMIT/OFFSET` | PostGIS geometry columns: enables spatial queries but requires Postgres+PostGIS even for local runs |
| **Measurements computed at upload and stored** | `GET` is a cheap read; results are stable | Compute on read: always fresh, but repeats the projection work on every request |
| **Original upload kept on disk** | Allows reprocessing if the measurement logic changes | Delete after processing: smaller footprint |
| **Transformers cached per request, not globally** | pyproj transformers aren't guaranteed thread-safe and FastAPI runs sync endpoints in a thread pool | Global `lru_cache`: faster, riskier |

**Security notes:** filenames are sanitised; uploads are size-capped while streaming; zips are checked for
path traversal (zip-slip) and total expanded size before extraction; internal error details are logged, not returned.

## Limitations

- Features that cross the antimeridian (±180°) can be assigned the wrong UTM zone.
- A very large polygon spanning many UTM zones is measured in one zone and will show a `notes` warning;
  use `geodesic_value` for those.
- `GeometryCollection` / KML `MultiGeometry` is reported as `UNSUPPORTED` rather than split into parts.
- Shapefiles must include `.shp`, `.shx` and `.dbf`. `.kmz` is not accepted.
- Processing is synchronous, so a very large file holds the request open.

## Learnings

- Area in degrees is meaningless, and even after projecting, the projection matters: UTM is not equal-area, so
  returning a geodesic number beside it made the error visible (about 0.1% on the sample file).
- KML coordinates are almost always 3D (lon, lat, altitude); Z needs dropping before 2D projection and area logic.
- Hostile uploads are a real input class: zip-slip and zip bombs need handling before extraction, not after.
- Real files are messy: invalid (self-intersecting) polygons, null geometries, mixed geometry types in a KML
  and missing `.prj` files all show up, so every feature returns an explicit status instead of raising.
- SQLite drops timezone info on read, which made `created_at` serialize inconsistently until it was normalised.

## Future scope

- **Async processing:** move steps 3-5 of the flow to a worker queue; `POST` returns `202` with `PROCESSING`, clients poll `GET /api/files/{id}/`.
- **PostGIS** storage for spatial queries (features within a bounding box, intersections).
- **KMZ and GeoJSON** input; split `GeometryCollection` into measurable parts.
- Per-request **CRS override** for files with a missing or wrong `.prj`.
- Authentication, per-user file ownership and upload quotas; a retention/cleanup job for stored uploads.
- Streaming/chunked ingestion for files with millions of features; export measurements as CSV.
- Alembic migrations instead of `create_all`; CI running the test suite.

"""CRS handling and measurement.

Measurements are NEVER computed on raw lon/lat degrees. For every feature we:
  1. bring the geometry to WGS84 lon/lat (if it isn't already),
  2. pick a metric CRS from the feature's centroid (UTM zone, or a polar
     stereographic CRS near the poles),
  3. project and measure in metres / square metres,
  4. cross-check against a geodesic (ellipsoidal) calculation.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Iterator

import numpy as np
import shapely
from pyproj import CRS, Geod, Transformer
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

from app.errors import IngestError

log = logging.getLogger(__name__)

POLYGONAL = {"Polygon", "MultiPolygon"}
LINEAR = {"LineString", "MultiLineString"}
POINTAL = {"Point", "MultiPoint"}

ARCTIC_POLAR_EPSG = 3995  # WGS 84 / Arctic Polar Stereographic
ANTARCTIC_POLAR_EPSG = 3031  # WGS 84 / Antarctic Polar Stereographic

# Warn when projected and geodesic results disagree by more than this.
DEVIATION_WARN_PCT = 1.0


# ---------------------------------------------------------------- CRS helpers
def crs_label(crs: CRS) -> str:
    auth = crs.to_authority()
    if auth:
        return f"{auth[0]}:{auth[1]}"
    return crs.name or "UNKNOWN"


def resolve_crs(gdf) -> tuple[CRS, bool]:
    """Return (crs, assumed). Missing CRS -> WGS84 *only if* coords look like lon/lat."""
    if gdf.crs is not None:
        return CRS.from_user_input(gdf.crs), False
    minx, miny, maxx, maxy = gdf.total_bounds
    if not any(math.isnan(v) for v in (minx, miny, maxx, maxy)):
        if not (-180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90):
            raise IngestError(
                "File has no CRS and its coordinates are not valid longitude/latitude, "
                "so the CRS cannot be assumed. Add a .prj file (shapefile) or reproject."
            )
    return CRS.from_epsg(4326), True


def select_projected_epsg(lon: float, lat: float) -> int:
    """Pick a metric CRS for a WGS84 location: UTM zone, or polar stereographic."""
    if lat >= 84:
        return ARCTIC_POLAR_EPSG
    if lat < -80:
        return ANTARCTIC_POLAR_EPSG
    zone = min(max(int((lon + 180) // 6) + 1, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


# ------------------------------------------------------------------ utilities
def _reproject(geom: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    def fn(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, fn)


def _iter_polygons(geom: BaseGeometry) -> Iterator[Polygon]:
    if isinstance(geom, Polygon):
        if not geom.is_empty:
            yield geom
    elif hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _iter_polygons(part)


def _iter_lines(geom: BaseGeometry) -> Iterator[LineString]:
    if isinstance(geom, LineString):
        if not geom.is_empty:
            yield geom
    elif hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _iter_lines(part)


def _result(
    status: str,
    kind: str | None = None,
    value: float | None = None,
    unit: str | None = None,
    geodesic: float | None = None,
    epsg: int | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,  # OK | NOT_APPLICABLE | UNSUPPORTED | SKIPPED | ERROR
        "type": kind,  # "area" | "length" | None
        "value": value,
        "unit": unit,  # "m2" | "m"
        "geodesic_value": geodesic,
        "projected_crs": f"EPSG:{epsg}" if epsg else None,
        "notes": notes or [],
    }


# ------------------------------------------------------------------- measurer
class Measurer:
    """Measures features that share one source CRS.

    Transformers are cached per instance (one instance per layer per request)
    rather than globally, because pyproj Transformers are not guaranteed to be
    thread-safe and FastAPI runs sync endpoints in a thread pool.
    """

    def __init__(self, src_crs: CRS):
        self._src = src_crs
        self._to_wgs84 = (
            None
            if src_crs.to_epsg() == 4326
            else Transformer.from_crs(src_crs, 4326, always_xy=True)
        )
        self._to_metric: dict[int, Transformer] = {}
        self._geod = Geod(ellps="WGS84")

    # -- public ---------------------------------------------------------
    def measure(self, geom: BaseGeometry | None) -> dict[str, Any]:
        """Never raises: any problem is reported in the returned dict."""
        if geom is None or geom.is_empty:
            return _result("SKIPPED", notes=["Feature has no geometry"])
        gtype = geom.geom_type
        if gtype in POINTAL:
            return _result("NOT_APPLICABLE", notes=[f"No measurement is defined for {gtype}"])
        if gtype not in POLYGONAL | LINEAR:
            return _result(
                "UNSUPPORTED",
                notes=[f"Geometry type {gtype} is not supported for measurement"],
            )
        try:
            return self._measure_supported(geom)
        except Exception as exc:  # one bad feature must not sink the file
            log.warning("Measurement failed for a %s: %s", gtype, exc)
            return _result("ERROR", notes=[f"Measurement failed: {exc}"])

    # -- internals ------------------------------------------------------
    def _metric_transformer(self, epsg: int) -> Transformer:
        if epsg not in self._to_metric:
            self._to_metric[epsg] = Transformer.from_crs(4326, epsg, always_xy=True)
        return self._to_metric[epsg]

    def _measure_supported(self, geom: BaseGeometry) -> dict[str, Any]:
        notes: list[str] = []
        geom = shapely.force_2d(geom)  # KML carries altitude; irrelevant here
        is_area = geom.geom_type in POLYGONAL

        if is_area and not geom.is_valid:
            geom = make_valid(geom)
            notes.append("Invalid geometry was repaired with make_valid before measuring")

        wgs = _reproject(geom, self._to_wgs84) if self._to_wgs84 else geom
        centroid = wgs.centroid
        epsg = select_projected_epsg(centroid.x, centroid.y)
        metric = _reproject(wgs, self._metric_transformer(epsg))

        if is_area:
            kind, unit = "area", "m2"
            value = metric.area
            geodesic = self._geodesic_area(wgs)
        else:
            kind, unit = "length", "m"
            value = metric.length
            geodesic = self._geodesic_length(wgs)

        if not math.isfinite(value):
            raise ValueError("projection produced a non-finite result")

        if geodesic:
            dev = abs(value - geodesic) / geodesic * 100
            if dev > DEVIATION_WARN_PCT:
                notes.append(
                    f"Projected and geodesic values differ by {dev:.2f}% "
                    "(very large extent or geometry spans several UTM zones)"
                )

        return _result("OK", kind, round(value, 3), unit, round(geodesic, 3), epsg, notes)

    def _geodesic_area(self, wgs: BaseGeometry) -> float:
        def ring_area(ring) -> float:
            xy = shapely.get_coordinates(ring)
            area, _ = self._geod.polygon_area_perimeter(xy[:, 0], xy[:, 1])
            return abs(area)

        return sum(
            ring_area(p.exterior) - sum(ring_area(r) for r in p.interiors)
            for p in _iter_polygons(wgs)
        )

    def _geodesic_length(self, wgs: BaseGeometry) -> float:
        total = 0.0
        for line in _iter_lines(wgs):
            xy = shapely.get_coordinates(line)
            total += self._geod.line_length(xy[:, 0], xy[:, 1])
        return total

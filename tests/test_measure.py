"""Unit tests for CRS selection and measurement (no HTTP)."""
import math

import pytest
from pyproj import CRS
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon

from app.services.measure import Measurer, select_projected_epsg

WGS84 = CRS.from_epsg(4326)


def square(lon, lat, d=0.01):
    return Polygon([(lon, lat), (lon + d, lat), (lon + d, lat + d), (lon, lat + d)])


@pytest.mark.parametrize(
    "lon,lat,epsg",
    [
        (77.0, 28.0, 32643),   # northern hemisphere UTM 43N
        (-70.0, -33.0, 32719),  # southern hemisphere UTM 19S
        (180.0, 0.0, 32660),   # clamp to zone 60, not 61
        (-180.0, 10.0, 32601),
        (0.0, 86.0, 3995),     # Arctic polar stereographic
        (0.0, -85.0, 3031),    # Antarctic polar stereographic
    ],
)
def test_projected_crs_selection(lon, lat, epsg):
    assert select_projected_epsg(lon, lat) == epsg


def test_polygon_area_is_in_square_metres_not_degrees():
    r = Measurer(WGS84).measure(square(77.0, 28.0))
    assert r["status"] == "OK" and r["type"] == "area" and r["unit"] == "m2"
    # ~983 m x ~1106 m; a naive degree calculation would give 0.0001
    assert 1.07e6 < r["value"] < 1.10e6
    assert r["projected_crs"] == "EPSG:32643"
    # projected and geodesic should agree closely for a small polygon
    assert r["value"] == pytest.approx(r["geodesic_value"], rel=5e-3)


def test_area_matches_spherical_formula():
    # Independent check: spherical zone area R^2 * dlon * (sin lat2 - sin lat1)
    R = 6371008.8
    expected = R**2 * math.radians(0.01) * (math.sin(math.radians(28.01)) - math.sin(math.radians(28.0)))
    r = Measurer(WGS84).measure(square(77.0, 28.0))
    assert r["value"] == pytest.approx(expected, rel=1e-2)


def test_line_length_in_metres():
    r = Measurer(WGS84).measure(LineString([(77, 28), (77, 28.01)]))
    assert r["type"] == "length" and r["unit"] == "m"
    assert 1100 < r["value"] < 1115
    assert r["value"] == pytest.approx(r["geodesic_value"], rel=5e-3)


def test_points_need_no_measurement():
    r = Measurer(WGS84).measure(Point(77, 28))
    assert r["status"] == "NOT_APPLICABLE" and r["value"] is None


def test_unsupported_geometry_is_handled_gracefully():
    gc = GeometryCollection([Point(77, 28), LineString([(77, 28), (77, 28.01)])])
    r = Measurer(WGS84).measure(gc)
    assert r["status"] == "UNSUPPORTED" and r["value"] is None
    assert "GeometryCollection" in r["notes"][0]


def test_missing_geometry_is_skipped():
    assert Measurer(WGS84).measure(None)["status"] == "SKIPPED"
    assert Measurer(WGS84).measure(Polygon())["status"] == "SKIPPED"


def test_multipolygon_is_sum_of_parts():
    a, b = square(77.0, 28.0), square(77.1, 28.1)
    m = Measurer(WGS84)
    total = m.measure(MultiPolygon([a, b]))["value"]
    assert total == pytest.approx(m.measure(a)["value"] + m.measure(b)["value"], rel=1e-6)


def test_polygon_hole_is_subtracted():
    outer = [(77, 28), (77.02, 28), (77.02, 28.02), (77, 28.02)]
    hole = [(77.005, 28.005), (77.015, 28.005), (77.015, 28.015), (77.005, 28.015)]
    m = Measurer(WGS84)
    with_hole = m.measure(Polygon(outer, [hole]))
    expected = m.measure(Polygon(outer))["value"] - m.measure(Polygon(hole))["value"]
    assert with_hole["value"] == pytest.approx(expected, rel=1e-6)
    expected_geo = m.measure(Polygon(outer))["geodesic_value"] - m.measure(Polygon(hole))["geodesic_value"]
    assert with_hole["geodesic_value"] == pytest.approx(expected_geo, rel=1e-6)


def test_invalid_bowtie_polygon_is_repaired():
    bowtie = Polygon([(77, 28), (77.01, 28.01), (77.01, 28), (77, 28.01)])
    assert not bowtie.is_valid
    r = Measurer(WGS84).measure(bowtie)
    assert r["status"] == "OK" and r["value"] > 0
    assert any("repaired" in n for n in r["notes"])


def test_3d_coordinates_are_ignored():
    p = Polygon([(77, 28, 500), (77.01, 28, 510), (77.01, 28.01, 520), (77, 28.01, 530)])
    assert Measurer(WGS84).measure(p)["value"] == pytest.approx(
        Measurer(WGS84).measure(square(77.0, 28.0))["value"], rel=1e-9
    )


def test_projected_source_crs_is_reprojected_before_measuring():
    # 1 km x 1 km square in UTM 43N: area should come out as exactly 1,000,000 m2
    utm43 = CRS.from_epsg(32643)
    sq = Polygon([(500000, 3100000), (501000, 3100000), (501000, 3101000), (500000, 3101000)])
    r = Measurer(utm43).measure(sq)
    assert r["value"] == pytest.approx(1_000_000, rel=1e-6)
    assert r["projected_crs"] == "EPSG:32643"


def test_failure_in_one_feature_is_reported_not_raised(monkeypatch):
    m = Measurer(WGS84)
    monkeypatch.setattr(m, "_measure_supported", lambda g: (_ for _ in ()).throw(RuntimeError("boom")))
    r = m.measure(square(77.0, 28.0))
    assert r["status"] == "ERROR" and "boom" in r["notes"][0]

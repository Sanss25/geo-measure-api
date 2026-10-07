"""End-to-end tests through the HTTP API."""
import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from app.config import settings
from app.services.measure import Measurer
from tests.conftest import KML, shapefile_zip, upload, zip_bytes


# ---------------------------------------------------------------- happy paths
def test_upload_kml_and_get_info(client):
    r = upload(client, "survey.kml", KML.encode())
    assert r.status_code == 201
    body = r.json()
    assert body["filename"] == "survey.kml"
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 3
    assert body["crs"] == "EPSG:4326"
    assert body["crs_assumed"] is False

    info = client.get(f"/api/files/{body['id']}/")
    assert info.status_code == 200 and info.json() == body


def test_features_endpoint_returns_geometry_crs_and_properties(client):
    fid = upload(client, "survey.kml", KML.encode()).json()["id"]
    data = client.get(f"/api/files/{fid}/features/").json()
    assert data["total"] == 3
    first = data["results"][0]
    assert first["index"] == 0
    assert first["geometry_type"] == "Polygon"
    assert first["crs"] == "EPSG:4326"
    assert first["geometry"]["type"] == "Polygon"
    assert first["properties"]["Name"] == "Square"
    assert [f["geometry_type"] for f in data["results"]] == ["Polygon", "LineString", "Point"]


def test_measurements_for_mixed_geometry_kml(client):
    fid = upload(client, "survey.kml", KML.encode()).json()["id"]
    data = client.get(f"/api/files/{fid}/measurements/").json()
    poly, line, point = (r["measurement"] for r in data["results"])
    assert poly["type"] == "area" and 1.07e6 < poly["value"] < 1.10e6
    assert line["type"] == "length" and 1100 < line["value"] < 1115
    assert point["status"] == "NOT_APPLICABLE"
    s = data["summary"]
    assert (s["total_features"], s["ok"], s["not_applicable"]) == (3, 2, 1)
    assert s["total_area_m2"] == pytest.approx(poly["value"])
    assert s["total_length_m"] == pytest.approx(line["value"])


def test_upload_shapefile_zip(client, tmp_path, mixed_gdf):
    # mixed geometry types can't share one shapefile, so use polygon + a second layer
    polys = mixed_gdf.iloc[:1]
    r = upload(client, "areas.zip", shapefile_zip(polys, tmp_path))
    assert r.status_code == 201
    assert r.json()["crs"] == "EPSG:4326"
    fid = r.json()["id"]
    m = client.get(f"/api/files/{fid}/measurements/").json()["results"][0]
    assert m["properties"]["name"] == "poly"
    assert 1.07e6 < m["measurement"]["value"] < 1.10e6


def test_projected_shapefile_keeps_its_crs_and_measures_in_metres(client, tmp_path):
    gdf = gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[Polygon([(500000, 3100000), (501000, 3100000), (501000, 3101000), (500000, 3101000)])],
        crs="EPSG:32643",
    )
    r = upload(client, "utm.zip", shapefile_zip(gdf, tmp_path))
    assert r.json()["crs"] == "EPSG:32643"
    m = client.get(f"/api/files/{r.json()['id']}/measurements/").json()["results"][0]
    assert m["measurement"]["value"] == pytest.approx(1_000_000, rel=1e-6)


def test_shapefile_without_prj_assumes_wgs84_and_says_so(client, tmp_path, square_gdf):
    no_crs = square_gdf.set_crs(None, allow_override=True)
    r = upload(client, "nocrs.zip", shapefile_zip(no_crs, tmp_path))
    assert r.status_code == 201
    assert r.json()["crs"] == "EPSG:4326" and r.json()["crs_assumed"] is True


def test_pagination(client):
    fid = upload(client, "survey.kml", KML.encode()).json()["id"]
    page = client.get(f"/api/files/{fid}/features/?limit=2&offset=2").json()
    assert page["total"] == 3 and len(page["results"]) == 1
    assert page["results"][0]["index"] == 2
    m = client.get(f"/api/files/{fid}/measurements/?limit=1").json()
    assert len(m["results"]) == 1 and m["summary"]["total_features"] == 3  # totals ignore paging


# ------------------------------------------------------------------ rejection
def test_unsupported_extension_is_415(client):
    assert upload(client, "data.geojson", b"{}").status_code == 415


def test_oversized_upload_is_413(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 10)
    assert upload(client, "survey.kml", KML.encode()).status_code == 413


def test_zip_without_shp_is_422(client):
    r = upload(client, "x.zip", zip_bytes({"readme.txt": b"hi"}))
    assert r.status_code == 422
    assert ".shp" in r.json()["detail"]["error"]


def test_shapefile_missing_dbf_is_422(client, tmp_path, square_gdf):
    gdf_zip = shapefile_zip(square_gdf, tmp_path)
    import io, zipfile
    src = zipfile.ZipFile(io.BytesIO(gdf_zip))
    stripped = zip_bytes({n: src.read(n) for n in src.namelist() if not n.endswith(".dbf")})
    r = upload(client, "x.zip", stripped)
    assert r.status_code == 422 and ".dbf" in r.json()["detail"]["error"]


def test_zip_slip_is_rejected(client, tmp_path):
    r = upload(client, "evil.zip", zip_bytes({"../evil.shp": b"x"}))
    assert r.status_code == 422
    assert "unsafe" in r.json()["detail"]["error"]
    assert not (settings.upload_dir.parent / "evil.shp").exists()


def test_corrupt_zip_is_422(client):
    assert upload(client, "bad.zip", b"not a zip").status_code == 422


def test_unprojected_coordinates_without_crs_are_rejected(client, tmp_path):
    gdf = gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[Polygon([(500000, 3100000), (501000, 3100000), (501000, 3101000)])],
        crs=None,
    )
    r = upload(client, "mystery.zip", shapefile_zip(gdf, tmp_path))
    assert r.status_code == 422 and "no CRS" in r.json()["detail"]["error"]


def test_garbage_kml_fails_and_failure_is_queryable(client):
    r = upload(client, "bad.kml", b"this is not xml")
    assert r.status_code == 422
    fid = r.json()["detail"]["id"]
    info = client.get(f"/api/files/{fid}/").json()
    assert info["status"] == "FAILED" and info["error"]
    # results of a failed file are not available
    assert client.get(f"/api/files/{fid}/measurements/").status_code == 409


def test_kml_with_no_features_fails_cleanly(client):
    empty = '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>'
    r = upload(client, "empty.kml", empty.encode())
    assert r.status_code == 422 and "No features" in r.json()["detail"]["error"]


def test_unknown_id_is_404(client):
    for path in ("", "features/", "measurements/"):
        assert client.get(f"/api/files/nope/{path}").status_code == 404


# ----------------------------------------------------------- partial failure
def test_one_failing_feature_does_not_fail_the_file(client, monkeypatch):
    original = Measurer._measure_supported

    def flaky(self, geom):
        if geom.geom_type == "LineString":
            raise RuntimeError("boom")
        return original(self, geom)

    monkeypatch.setattr(Measurer, "_measure_supported", flaky)
    r = upload(client, "survey.kml", KML.encode())
    assert r.status_code == 201 and r.json()["status"] == "COMPLETED"
    data = client.get(f"/api/files/{r.json()['id']}/measurements/").json()
    statuses = [x["measurement"]["status"] for x in data["results"]]
    assert statuses == ["OK", "ERROR", "NOT_APPLICABLE"]
    assert data["summary"]["error"] == 1 and data["summary"]["ok"] == 1


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}

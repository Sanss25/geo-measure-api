import io
import os
import tempfile
import zipfile
from pathlib import Path

# Must run before `app` is imported so the engine/paths point at a scratch dir.
_tmp = tempfile.mkdtemp(prefix="geoapi-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["UPLOAD_DIR"] = f"{_tmp}/uploads"

import geopandas as gpd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from shapely.geometry import LineString, Point, Polygon  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

# 0.01 deg x 0.01 deg square near (77E, 28N): roughly 983 m x 1106 m.
KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>Square</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
77.00,28.00,0 77.01,28.00,0 77.01,28.01,0 77.00,28.01,0 77.00,28.00,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>Road</name><LineString><coordinates>77.0,28.0,0 77.0,28.01,0</coordinates></LineString></Placemark>
<Placemark><name>Gate</name><Point><coordinates>77.005,28.005,0</coordinates></Point></Placemark>
</Document></kml>"""


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as c:
        yield c


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def shapefile_zip(gdf: gpd.GeoDataFrame, tmp_path: Path, name: str = "layer") -> bytes:
    folder = tmp_path / "shp"
    folder.mkdir(exist_ok=True)
    gdf.to_file(folder / f"{name}.shp")
    return zip_bytes({p.name: p.read_bytes() for p in folder.iterdir()})


def upload(client, filename: str, data: bytes):
    return client.post("/api/files/", files={"file": (filename, data)})


@pytest.fixture()
def square_gdf():
    return gpd.GeoDataFrame(
        {"name": ["sq"]},
        geometry=[Polygon([(77, 28), (77.01, 28), (77.01, 28.01), (77, 28.01)])],
        crs="EPSG:4326",
    )


@pytest.fixture()
def mixed_gdf():
    return gpd.GeoDataFrame(
        {"name": ["poly", "line", "pt"]},
        geometry=[
            Polygon([(77, 28), (77.01, 28), (77.01, 28.01), (77, 28.01)]),
            LineString([(77, 28), (77, 28.01)]),
            Point(77.005, 28.005),
        ],
        crs="EPSG:4326",
    )

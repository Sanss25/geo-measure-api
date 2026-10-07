"""Regenerate samples/sample_parcels.zip: two parcels in a *projected* CRS (EPSG:32643)."""
import shutil
import tempfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon

parcels = gpd.GeoDataFrame(
    {"parcel": ["A", "B"], "owner": ["Demo Trust", "Demo Co-op"]},
    geometry=[
        Polygon([(780000, 1433000), (780200, 1433000), (780200, 1433150), (780000, 1433150)]),
        Polygon([(780300, 1433000), (780500, 1433000), (780500, 1433100), (780300, 1433100)]),
    ],
    crs="EPSG:32643",
)
out = Path(__file__).resolve().parent.parent / "samples" / "sample_parcels"
with tempfile.TemporaryDirectory() as tmp:
    parcels.to_file(Path(tmp) / "parcels.shp")
    shutil.make_archive(str(out), "zip", tmp)
print("wrote", out.with_suffix(".zip"))

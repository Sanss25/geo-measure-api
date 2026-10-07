"""Getting bytes onto disk safely and turning them into GeoDataFrames."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import BinaryIO, Iterator

import geopandas as gpd
import pyogrio
from pyogrio.errors import DataLayerError, DataSourceError

from app.config import settings
from app.errors import IngestError, UploadTooLarge

ALLOWED_SUFFIXES = {".zip", ".kml"}
SHP_SIDECARS = (".shx", ".dbf")
_CHUNK = 1024 * 1024


def safe_filename(name: str | None) -> str:
    """Strip any path and odd characters from a client-supplied filename."""
    base = Path((name or "upload").replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(". ")
    return (base or "upload")[:200]


def save_upload(src: BinaryIO, dest: Path) -> int:
    """Stream to disk, enforcing the size cap while reading (not after)."""
    written = 0
    with dest.open("wb") as out:
        while chunk := src.read(_CHUNK):
            written += len(chunk)
            if written > settings.max_upload_bytes:
                raise UploadTooLarge
            out.write(chunk)
    return written


def extract_zip_safely(zip_path: Path, dest: Path) -> list[Path]:
    """Extract a zip, rejecting path traversal and zip bombs. Returns the .shp paths."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise IngestError("Invalid or corrupt zip archive") from exc

    with zf:
        members = [
            m for m in zf.infolist()
            if not m.is_dir() and not m.filename.startswith("__MACOSX/")
        ]
        if sum(m.file_size for m in members) > settings.max_uncompressed_bytes:
            raise IngestError("Zip archive expands beyond the allowed size")

        root = dest.resolve()
        for m in members:
            target = (dest / m.filename).resolve()
            if not target.is_relative_to(root):  # zip-slip
                raise IngestError("Zip archive contains an unsafe file path")
        zf.extractall(dest, members)

    shp_files = sorted(
        p for p in dest.rglob("*") if p.is_file() and p.suffix.lower() == ".shp"
    )
    if not shp_files:
        raise IngestError("Zip archive does not contain a .shp file")
    return shp_files


def _check_sidecars(shp: Path) -> None:
    siblings = {p.name.lower() for p in shp.parent.iterdir()}
    missing = [e for e in SHP_SIDECARS if (shp.stem + e).lower() not in siblings]
    if missing:
        raise IngestError(
            f"Shapefile '{shp.name}' is missing required file(s): {', '.join(missing)}"
        )


def _read(path: Path, **kwargs) -> gpd.GeoDataFrame:
    try:
        return gpd.read_file(path, **kwargs)
    except (DataSourceError, DataLayerError) as exc:
        raise IngestError(f"Could not read geospatial data: {str(exc)[:300]}") from exc


def iter_layers(path: Path, workdir: Path) -> Iterator[tuple[str, gpd.GeoDataFrame]]:
    """Yield (layer_name, GeoDataFrame) for every layer in an upload.

    A generator, so only one layer is held in memory at a time. `workdir` must
    outlive iteration because zips are extracted there.
    """
    suffix = path.suffix.lower()
    if suffix == ".kml":
        try:
            layers = pyogrio.list_layers(path)
        except (DataSourceError, DataLayerError) as exc:
            raise IngestError(f"Could not read KML: {str(exc)[:300]}") from exc
        for name in layers[:, 0]:
            yield str(name), _read(path, layer=str(name))
    elif suffix == ".zip":
        for shp in extract_zip_safely(path, workdir):
            _check_sidecars(shp)
            yield shp.stem, _read(shp)
    else:  # defensive: the router already filters
        raise IngestError(f"Unsupported file type '{suffix}'")

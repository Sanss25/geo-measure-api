"""Orchestrates: read file -> measure each feature -> persist."""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.errors import IngestError
from app.models import COMPLETED, FAILED, Feature, UploadedFile
from app.services.ingest import iter_layers
from app.services.measure import Measurer, crs_label, resolve_crs

log = logging.getLogger(__name__)


def process_upload(db: Session, record: UploadedFile, path: Path) -> UploadedFile:
    """Process a stored upload. Always leaves `record` COMPLETED or FAILED."""
    try:
        _ingest(db, record, path)
    except IngestError as exc:
        db.rollback()
        _mark_failed(db, record.id, str(exc))
    except Exception:
        log.exception("Unexpected error processing file %s", record.id)
        db.rollback()
        _mark_failed(db, record.id, "Unexpected error while processing the file")
    return db.get(UploadedFile, record.id)


def _mark_failed(db: Session, file_id: str, message: str) -> None:
    rec = db.get(UploadedFile, file_id)
    rec.status = FAILED
    rec.error = message
    db.commit()


def _ingest(db: Session, record: UploadedFile, path: Path) -> None:
    idx = 0
    labels: list[str] = []
    assumed_any = False

    with tempfile.TemporaryDirectory(prefix="geo-") as tmp:
        for layer_name, gdf in iter_layers(path, Path(tmp)):
            if gdf.empty:
                continue
            crs, assumed = resolve_crs(gdf)
            label = crs_label(crs)
            if label not in labels:
                labels.append(label)
            assumed_any = assumed_any or assumed

            measurer = Measurer(crs)
            # GeoJSON-style dicts: JSON-safe properties (NaN -> null, dates -> ISO)
            gj_features = json.loads(gdf.to_json(na="null", drop_id=True))["features"]

            rows = []
            for geom, gj in zip(gdf.geometry, gj_features):
                if idx >= settings.max_features:
                    raise IngestError(
                        f"File has more than {settings.max_features} features"
                    )
                rows.append(
                    Feature(
                        file_id=record.id,
                        idx=idx,
                        layer=layer_name,
                        geometry_type=geom.geom_type if geom is not None else None,
                        geometry=gj["geometry"],
                        crs=label,
                        crs_assumed=assumed,
                        properties=gj["properties"] or {},
                        measurement=measurer.measure(geom),
                    )
                )
                idx += 1
            db.add_all(rows)

    if idx == 0:
        raise IngestError("No features found in file")

    record.feature_count = idx
    record.crs = labels[0] if len(labels) == 1 else "MIXED"
    record.crs_assumed = assumed_any
    record.status = COMPLETED
    record.error = None
    db.commit()  # features + status land atomically

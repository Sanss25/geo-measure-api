from __future__ import annotations

import shutil
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.errors import UploadTooLarge
from app.models import COMPLETED, FAILED, PROCESSING, Feature, UploadedFile
from app.schemas import (
    FeatureList,
    FeatureMeasurement,
    FeatureOut,
    FileInfo,
    MeasurementList,
    MeasurementSummary,
)
from app.services.ingest import ALLOWED_SUFFIXES, safe_filename, save_upload
from app.services.processor import process_upload

router = APIRouter(prefix="/api/files", tags=["files"])


def _get_file(db: Session, file_id: str) -> UploadedFile:
    rec = db.get(UploadedFile, file_id)
    if rec is None:
        raise HTTPException(404, "File not found")
    return rec


def _require_completed(rec: UploadedFile) -> None:
    if rec.status != COMPLETED:
        raise HTTPException(
            409, detail=f"File is {rec.status}" + (f": {rec.error}" if rec.error else "")
        )


@router.post("/", response_model=FileInfo, status_code=201)
def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload a .kml or a .zip containing a Shapefile; it is processed before responding."""
    filename = safe_filename(file.filename)
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(415, "Unsupported file type. Upload a .kml or a .zip Shapefile.")

    file_id = uuid.uuid4().hex
    folder = settings.upload_dir / file_id
    folder.mkdir(parents=True)
    stored = folder / filename
    try:
        save_upload(file.file, stored)
    except UploadTooLarge:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(
            413, f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit"
        )

    record = UploadedFile(id=file_id, filename=filename, status=PROCESSING)
    db.add(record)
    db.commit()

    record = process_upload(db, record, stored)
    if record.status == FAILED:
        raise HTTPException(
            422, detail={"id": record.id, "status": record.status, "error": record.error}
        )
    return record


@router.get("/{file_id}/", response_model=FileInfo)
def get_file(file_id: str, db: Session = Depends(get_db)):
    return _get_file(db, file_id)


@router.get("/{file_id}/features/", response_model=FeatureList)
def list_features(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Every feature with its geometry (GeoJSON, in the file's own CRS) and attributes."""
    rec = _get_file(db, file_id)
    _require_completed(rec)
    rows = db.scalars(
        select(Feature)
        .where(Feature.file_id == file_id)
        .order_by(Feature.idx)
        .limit(limit)
        .offset(offset)
    ).all()
    return FeatureList(
        file_id=file_id,
        total=rec.feature_count,
        limit=limit,
        offset=offset,
        results=[
            FeatureOut(
                index=f.idx,
                layer=f.layer,
                geometry_type=f.geometry_type,
                crs=f.crs,
                crs_assumed=f.crs_assumed,
                geometry=f.geometry,
                properties=f.properties,
            )
            for f in rows
        ],
    )


@router.get("/{file_id}/measurements/", response_model=MeasurementList)
def list_measurements(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Per-feature measurements plus file-wide totals (totals ignore pagination)."""
    rec = _get_file(db, file_id)
    _require_completed(rec)

    summary = _summarise(db.scalars(select(Feature.measurement).where(Feature.file_id == file_id)))
    rows = db.scalars(
        select(Feature)
        .where(Feature.file_id == file_id)
        .order_by(Feature.idx)
        .limit(limit)
        .offset(offset)
    ).all()
    return MeasurementList(
        file_id=file_id,
        total=rec.feature_count,
        limit=limit,
        offset=offset,
        summary=summary,
        results=[
            FeatureMeasurement(
                index=f.idx,
                layer=f.layer,
                geometry_type=f.geometry_type,
                crs=f.crs,
                properties=f.properties,
                measurement=f.measurement,
            )
            for f in rows
        ],
    )


def _summarise(measurements) -> MeasurementSummary:
    counts = {"OK": 0, "NOT_APPLICABLE": 0, "UNSUPPORTED": 0, "SKIPPED": 0, "ERROR": 0}
    area = length = 0.0
    total = 0
    for m in measurements:
        total += 1
        counts[m["status"]] = counts.get(m["status"], 0) + 1
        if m["status"] == "OK":
            if m["type"] == "area":
                area += m["value"]
            elif m["type"] == "length":
                length += m["value"]
    return MeasurementSummary(
        total_features=total,
        ok=counts["OK"],
        not_applicable=counts["NOT_APPLICABLE"],
        unsupported=counts["UNSUPPORTED"],
        skipped=counts["SKIPPED"],
        error=counts["ERROR"],
        total_area_m2=round(area, 3),
        total_length_m=round(length, 3),
    )

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class FileInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    feature_count: int
    crs: str | None
    crs_assumed: bool
    status: str
    error: str | None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        # SQLite drops tzinfo on read; everything we store is UTC.
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v


class Measurement(BaseModel):
    status: str  # OK | NOT_APPLICABLE | UNSUPPORTED | SKIPPED | ERROR
    type: str | None
    value: float | None
    unit: str | None
    geodesic_value: float | None
    projected_crs: str | None
    notes: list[str]


class FeatureOut(BaseModel):
    index: int
    layer: str | None
    geometry_type: str | None
    crs: str
    crs_assumed: bool
    geometry: dict[str, Any] | None
    properties: dict[str, Any]


class FeatureMeasurement(BaseModel):
    index: int
    layer: str | None
    geometry_type: str | None
    crs: str
    properties: dict[str, Any]
    measurement: Measurement


class MeasurementSummary(BaseModel):
    total_features: int
    ok: int
    not_applicable: int
    unsupported: int
    skipped: int
    error: int
    total_area_m2: float
    total_length_m: float


class Page(BaseModel):
    file_id: str
    total: int
    limit: int
    offset: int


class FeatureList(Page):
    results: list[FeatureOut]


class MeasurementList(Page):
    summary: MeasurementSummary
    results: list[FeatureMeasurement]

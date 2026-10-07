from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UploadedFile(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=PROCESSING)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "EPSG:4326", or "MIXED" if layers disagree (see per-feature crs).
    crs: Mapped[str | None] = mapped_column(String(64), nullable=True)
    crs_assumed: Mapped[bool] = mapped_column(Boolean, default=False)
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    features: Mapped[list["Feature"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (Index("ix_features_file_idx", "file_id", "idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)  # 0-based position within the file
    layer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    geometry_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    geometry: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # GeoJSON, source CRS
    crs: Mapped[str] = mapped_column(String(64))
    crs_assumed: Mapped[bool] = mapped_column(Boolean, default=False)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    measurement: Mapped[dict] = mapped_column(JSON, default=dict)

    file: Mapped[UploadedFile] = relationship(back_populates="features")

"""Runtime settings, read from environment variables with sane defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _mb(name: str, default: int) -> int:
    return int(os.getenv(name, default)) * 1024 * 1024


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
    )
    upload_dir: Path = field(
        default_factory=lambda: Path(os.getenv("UPLOAD_DIR", "./data/uploads"))
    )
    # Hard cap on the uploaded file itself.
    max_upload_bytes: int = field(default_factory=lambda: _mb("MAX_UPLOAD_MB", 50))
    # Hard cap on what a .zip may expand to (zip-bomb guard).
    max_uncompressed_bytes: int = field(
        default_factory=lambda: _mb("MAX_UNCOMPRESSED_MB", 500)
    )
    max_features: int = field(
        default_factory=lambda: int(os.getenv("MAX_FEATURES", 100_000))
    )


settings = Settings()

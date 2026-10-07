import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import init_db
from app.routers import files

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Geospatial File Measurement API",
    description="Upload a Shapefile (.zip) or KML and get per-feature area/length.",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(files.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}

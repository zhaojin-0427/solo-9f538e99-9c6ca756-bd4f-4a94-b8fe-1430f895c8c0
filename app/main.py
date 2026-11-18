"""FastAPI application: upload relation revisions and read water polygons."""

from __future__ import annotations

import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .database import (
    AlreadyRegistered,
    Database,
    NoCurrentFace,
    PointOnBoundary,
    RevisionConflict,
)
from .geometry import GeometryError, build_polygon_geojson
from .models import (
    MonitorRegistration,
    MonitorRegistrationIn,
    RevisionIn,
    RevisionMonitoring,
    RevisionResult,
)

def _database_path() -> str:
    # Read lazily so tests (and redeployed env) can override FLOOD_DB_PATH.
    return os.environ.get("FLOOD_DB_PATH", "/data/flood.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database(_database_path())
    yield


app = FastAPI(
    title="OSM Flood Water-Area API",
    version=__version__,
    lifespan=lifespan,
    description=(
        "Submit complete, incrementing revisions of an OSM water relation "
        "(nodes + unordered outer/inner ways). Valid revisions become a "
        "GeoJSON Polygon; invalid revisions retain their raw input and an "
        "error, while the last valid polygon keeps being served. "
        "A relation may register 1-8 fixed monitoring points (immutable); "
        "every later revision then reports per-point inside/outside/pending "
        "states and records in/out events when a point's definite state flips."
    ),
)


def _db(request: Request) -> Database:
    return request.app.state.db


def _stored_to_result(row: dict) -> RevisionResult:
    return RevisionResult(
        relation_id=row["relation_id"],
        revision=row["revision"],
        status=row["status"],
        geometry=row["geometry"],
        error=row["error"],
    )


def _to_monitoring(data: dict | None) -> RevisionMonitoring | None:
    return RevisionMonitoring(**data) if data is not None else None


@app.get("/health", tags=["meta"])
def health(request: Request) -> dict:
    db = _db(request)
    return {"status": "ok", "version": __version__, "db": db.path}


@app.post(
    "/relations/{relation_id}/revisions",
    response_model=RevisionResult,
    status_code=200,
    tags=["revisions"],
    summary="Upload one complete relation revision",
)
def upload_revision(relation_id: int, payload: RevisionIn, request: Request) -> RevisionResult:
    """Validate and store a revision.

    The raw input is always stored atomically together with its computed
    geometry/state. Geometric failure yields ``status="invalid"`` (HTTP
    200, the revision is retained); only a non-incrementing revision
    number is rejected (HTTP 409, nothing is stored).
    """
    error = None
    geometry = None
    status = "valid"
    try:
        geometry = build_polygon_geojson(payload)
    except GeometryError as exc:
        status = "invalid"
        error = exc.to_detail()

    db = _db(request)
    try:
        monitoring = db.save_revision(
            relation_id=relation_id,
            revision=payload.revision,
            status=status,
            input_payload=payload.model_dump(),
            geometry=geometry,
            error=error,
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": str(exc),
                "latest_revision": exc.latest,
            },
        ) from exc

    last_valid = db.get_last_valid(relation_id)
    return RevisionResult(
        relation_id=relation_id,
        revision=payload.revision,
        status=status,
        geometry=geometry,
        error=error,
        last_valid_revision=last_valid["revision"] if last_valid else None,
        monitoring=_to_monitoring(monitoring),
    )


@app.get(
    "/relations/{relation_id}/revisions/{revision}",
    response_model=RevisionResult,
    tags=["revisions"],
    summary="Read input, geometry and status of one stored revision",
)
def read_revision(relation_id: int, revision: int, request: Request) -> RevisionResult:
    row = _db(request).get_revision(relation_id, revision)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "revision does not exist"},
        )
    result = _stored_to_result(row)
    result.input = row["input"]
    result.monitoring = _to_monitoring(_db(request).get_revision_monitoring(relation_id, revision))
    last_valid_row = _db(request).get_last_valid(relation_id)
    result.last_valid_revision = last_valid_row["revision"] if last_valid_row else None
    return result


@app.get(
    "/relations/{relation_id}/revisions",
    tags=["revisions"],
    summary="List all stored revisions of a relation",
)
def list_revisions(relation_id: int, request: Request) -> dict:
    return {"relation_id": relation_id, "revisions": _db(request).list_revisions(relation_id)}


@app.get(
    "/relations/{relation_id}/current",
    response_model=RevisionResult,
    tags=["revisions"],
    summary="Read the currently served (latest valid) polygon",
)
def read_current(relation_id: int, request: Request) -> RevisionResult:
    """Current face semantics.

    * latest revision valid  -> serve its polygon;
    * latest revision invalid -> stop serving: 409, but the response
      points at ``last_valid_revision`` which remains fully readable via
      the per-revision endpoint;
    * a later valid revision resumes serving automatically.
    """
    db = _db(request)
    latest = db.get_latest_revision(relation_id)
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "no revision stored for this relation"},
        )
    last_valid = db.get_last_valid(relation_id)
    monitoring = db.get_revision_monitoring(relation_id, latest["revision"])
    if latest["status"] == "valid":
        result = _stored_to_result(latest)
        result.last_valid_revision = last_valid["revision"] if last_valid else None
        result.monitoring = _to_monitoring(monitoring)
        return result

    return JSONResponse(
        status_code=409,
        content={
            "relation_id": relation_id,
            "revision": latest["revision"],
            "status": "invalid",
            "geometry": None,
            "error": latest["error"],
            "last_valid_revision": last_valid["revision"] if last_valid else None,
            "monitoring": monitoring,
            "detail": (
                "current revision is invalid and the face is not being served; "
                "fetch last_valid_revision for the previous valid polygon"
            ),
        },
    )


# ------------------------------------------------------------------ monitoring
@app.post(
    "/relations/{relation_id}/monitor-points",
    response_model=MonitorRegistration,
    status_code=200,
    tags=["monitoring"],
    summary="Register the immutable monitoring points of a relation",
)
def register_monitor_points(
    relation_id: int, payload: MonitorRegistrationIn, request: Request
) -> MonitorRegistration:
    """Register 1..8 uniquely numbered monitoring points.

    The inside/outside baseline is computed against the *currently served*
    face inside the registration transaction, so interleaved uploads and
    registrations are decided strictly by commit order. The whole group is
    rejected (nothing stored) when there is no valid current face, when the
    relation is already registered, or when any point lies exactly on the
    outer ring or a hole boundary.
    """
    db = _db(request)
    try:
        registration = db.register_monitor_points(
            relation_id, [p.model_dump() for p in payload.points]
        )
    except AlreadyRegistered as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "already_registered", "message": str(exc)},
        ) from exc
    except NoCurrentFace as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "no_current_face", "message": str(exc)},
        ) from exc
    except PointOnBoundary as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_on_boundary",
                "message": str(exc),
                "point_ids": exc.point_ids,
            },
        ) from exc
    return MonitorRegistration(**registration)


@app.get(
    "/relations/{relation_id}/monitor-points",
    response_model=MonitorRegistration,
    tags=["monitoring"],
    summary="Read the registered monitoring points and their baseline",
)
def read_monitor_points(relation_id: int, request: Request) -> MonitorRegistration:
    registration = _db(request).get_registration(relation_id)
    if registration is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "no monitoring points registered"},
        )
    return MonitorRegistration(**registration)


@app.get(
    "/relations/{relation_id}/monitor-events",
    tags=["monitoring"],
    summary="List all in/out events of the registered monitoring points",
)
def list_monitor_events(relation_id: int, request: Request) -> dict:
    db = _db(request)
    if db.get_registration(relation_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "no monitoring points registered"},
        )
    return {"relation_id": relation_id, "events": db.list_monitor_events(relation_id)}

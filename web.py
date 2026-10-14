"""Read-only public HTTP delivery. Ingestion owns expiry and media maintenance."""

from __future__ import annotations

import asyncio
import mimetypes
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from settings import ROOT, settings
from storage import (
    connect,
    get_event,
    init_db,
    list_events,
    status_summary,
    stream_revision,
)


class Media(BaseModel):
    url: str
    mime_type: str
    kind: str


class EvidenceRevision(BaseModel):
    revision: int
    text: str
    recorded_at: str


class SourcePost(BaseModel):
    id: int
    role: Literal["initial", "update", "comment"]
    source_name: str
    username: str | None = None
    posted_at: str
    edited_at: str | None = None
    text: str
    link: str | None = None
    revision: int = 1
    revisions: list[EvidenceRevision] = Field(default_factory=list)
    media: list[Media] = Field(default_factory=list)


class EventBrief(BaseModel):
    id: str
    kind: str
    severity: str
    title: str
    description: str
    location_label: str | None = None
    location_precision: str
    location_source: str | None = None
    location_confidence: float | None = Field(default=None, ge=0, le=1)
    latitude: float | None = None
    longitude: float | None = None
    radius_m: int | None = None
    first_reported_at: str
    last_confirmed_at: str
    expires_at: str
    updated_at: str
    revision: int = 1
    updates: int
    message_count: int
    source_count: int
    source_names: list[str] = Field(default_factory=list)
    source_name: str | None = None


class EventDetail(EventBrief):
    feed: list[SourcePost]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.media.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(title="MapExp", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = (
        "geolocation=(self), camera=(), microphone=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://*.tile.openstreetmap.org; connect-src 'self' https://*.tile.openstreetmap.org; font-src 'self'; media-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(
        ROOT / "static/index.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/api/config")
def config():
    return {
        "city_name": settings.city_name,
        "timezone": settings.timezone,
        "latitude": settings.latitude,
        "longitude": settings.longitude,
        "zoom": settings.zoom,
    }


@app.get("/healthz")
def health():
    with connect() as conn:
        conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/status")
def status():
    result = status_summary()
    heartbeat = result.get("heartbeat")
    try:
        age = (
            (datetime.now(UTC) - datetime.fromisoformat(heartbeat)).total_seconds()
            if heartbeat
            else None
        )
    except (ValueError, TypeError):
        age = None
    result["connected"] = (
        age is not None and 0 <= age < 60 and result.get("state") == "watching"
    )
    result["last_success_at"] = result.get("last_success")
    result["heartbeat_age_seconds"] = round(age) if age is not None else None
    return result


@app.get("/api/events", response_model=list[EventBrief])
def events_list():
    return list_events()


@app.get("/api/events/{event_id}", response_model=EventDetail)
def event_detail(event_id: str):
    result = get_event(event_id)
    if not result:
        raise HTTPException(404, "Event is unavailable or expired")
    return result


@app.get("/media/{filename:path}", include_in_schema=False)
def media_file(filename: str):
    path = (settings.media / filename).resolve()
    if not path.is_relative_to(settings.media.resolve()) or not path.is_file():
        raise HTTPException(404, "Media unavailable")
    with connect() as conn:
        media = conn.execute(
            "SELECT m.mime_type FROM media m JOIN event_posts ep ON ep.post_id=m.post_id JOIN events e ON e.id=ep.event_id WHERE m.local_path=? AND e.status='active' AND e.expires_at>? LIMIT 1",
            (filename, datetime.now(UTC).isoformat()),
        ).fetchone()
    if not media:
        raise HTTPException(404, "Media unavailable")
    mime = media[0] or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    inline = mime in {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "video/mp4",
        "video/webm",
        "audio/mpeg",
        "audio/ogg",
        "audio/mp4",
        "audio/wav",
    }
    return FileResponse(
        path,
        media_type=mime if inline else "application/octet-stream",
        filename=None if inline else path.name,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/stream")
async def stream(request: Request):
    async def updates():
        previous = None
        ticks = 0
        while not await request.is_disconnected():
            current = await asyncio.to_thread(stream_revision)
            if current != previous:
                previous = current
                yield f"event: events\ndata: {current}\n\n"
            elif ticks % 5 == 0:
                yield "event: heartbeat\ndata: alive\n\n"
            ticks += 1
            await asyncio.sleep(2)

    return StreamingResponse(
        updates(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

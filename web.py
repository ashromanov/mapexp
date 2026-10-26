"""Read-only public HTTP delivery. Ingestion owns expiry and media maintenance."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import mimetypes
import os
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, TypeAdapter

from settings import ROOT, settings
from storage import (
    connect,
    feed_snapshot,
    get_event,
    init_db,
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
    location_method: str | None = None
    location_reason: str | None = None


@dataclass(frozen=True)
class FeedSnapshot:
    revision: str
    body: bytes
    compressed: bytes
    etag: str
    gzip_etag: str


class FeedCache:
    """One validated/serialized snapshot per process, with single-flight rebuilds."""

    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot: FeedSnapshot | None = None
        self.adapter = TypeAdapter(list[EventBrief])

    def refresh(self) -> FeedSnapshot:
        with self.lock:
            revision = stream_revision()
            if self.snapshot is None or self.snapshot.revision != revision:
                # Token and rows must belong to the same read transaction.
                revision, items = feed_snapshot()
                body = self.adapter.dump_json(self.adapter.validate_python(items))
                compressed = gzip.compress(body, compresslevel=5, mtime=0)
                self.snapshot = FeedSnapshot(
                    revision,
                    body,
                    compressed,
                    '"' + hashlib.sha256(body).hexdigest() + '"',
                    '"' + hashlib.sha256(compressed).hexdigest() + '"',
                )
            return self.snapshot


class UpdateHub:
    """SSE subscribers wait in memory; they never poll SQLite individually."""

    def __init__(self, revision: str, interval: float = 2, heartbeat: float = 10):
        self.interval = interval
        self.heartbeat = heartbeat
        self.revision = revision
        self.condition = asyncio.Condition()
        self.closed = False
        self.available = True

    async def publish(self, revision: str | None):
        async with self.condition:
            changed = revision is not None and revision != self.revision
            recovered = revision is not None and not self.available
            self.available = revision is not None
            if revision is not None:
                self.revision = revision
            if changed or recovered or not self.available:
                self.condition.notify_all()

    async def wait(self, previous: str):
        async with self.condition:
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self.condition.wait_for(
                        lambda: (
                            self.closed
                            or not self.available
                            or self.revision != previous
                        )
                    ),
                    timeout=self.heartbeat,
                )
            return self.revision

    async def poll(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                snapshot = await asyncio.to_thread(feed_cache.refresh)
            except Exception:
                logging.getLogger(__name__).exception("Public feed refresh failed")
                await self.publish(None)
            else:
                await self.publish(snapshot.revision)


feed_cache = FeedCache()
update_hub: UpdateHub | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global feed_cache, update_hub
    settings.media.mkdir(parents=True, exist_ok=True)
    init_db()
    feed_cache = FeedCache()
    snapshot = await asyncio.to_thread(feed_cache.refresh)
    hub = update_hub = UpdateHub(snapshot.revision)
    task = asyncio.create_task(hub.poll())
    try:
        yield
    finally:
        async with hub.condition:
            hub.closed = True
            hub.condition.notify_all()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
            if request.query_params.get("v")
            else "public, max-age=3600"
        )
    if request.url.path.startswith("/api/") and request.url.path != "/api/events":
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
    return {"status": "ok", "version": "1.0.0", "revision": os.getenv("APP_REVISION", "development")}


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
def events_list(request: Request):
    snapshot = feed_cache.refresh()
    compressed = accepts_gzip(request.headers.get("accept-encoding", ""))
    etag = snapshot.gzip_etag if compressed else snapshot.etag
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "Vary": "Accept-Encoding",
    }
    matches = request.headers.get("if-none-match", "")
    if any(tag.strip().removeprefix("W/") in (etag, "*") for tag in matches.split(",")):
        return Response(status_code=304, headers=headers)
    if compressed:
        headers["Content-Encoding"] = "gzip"
    return Response(
        snapshot.compressed if compressed else snapshot.body,
        media_type="application/json",
        headers=headers,
    )


def accepts_gzip(value: str) -> bool:
    qualities = {}
    for encoding in value.lower().split(","):
        name, *parameters = encoding.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            if parameter.strip().startswith("q="):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0.0
        qualities[name] = quality
    return 0 < qualities.get("gzip", qualities.get("*", 0)) <= 1


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
    hub = update_hub
    if hub is None or not hub.available:
        return Response(status_code=503, headers={"Retry-After": "2"})

    async def updates():
        previous = None
        while not await request.is_disconnected():
            if hub.closed or not hub.available:
                return
            current = hub.revision
            if current != previous:
                previous = current
                yield f"event: events\ndata: {current}\n\n"
            else:
                yield "event: heartbeat\ndata: alive\n\n"
            await hub.wait(previous)

    return StreamingResponse(
        updates(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

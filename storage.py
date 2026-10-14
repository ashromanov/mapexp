"""Small SQLite store for the one-city MapExp instance."""

from __future__ import annotations

import json
import hashlib
import re
import math
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from settings import settings


DB_PATH = settings.database
_MIN_DISPLAY_MINUTES = {"info": 360, "warning": 720, "danger": 1_440, "critical": 1_440}


def now() -> str:
    return datetime.now(UTC).isoformat()


def effective_display_minutes(severity: str, requested_minutes: int) -> int:
    """Keep map cards long enough even when the model underestimates their TTL."""
    return max(_MIN_DISPLAY_MINUTES[severity], min(int(requested_minutes), 10_080))


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(path: Path | str = DB_PATH) -> None:
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                username TEXT,
                timezone TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES sources(id),
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                parent_post_id INTEGER REFERENCES posts(id),
                posted_at TEXT NOT NULL,
                edited_at TEXT,
                text TEXT NOT NULL DEFAULT '',
                original_text TEXT NOT NULL DEFAULT '',
                link TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                deleted_at TEXT,
                llm_status TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(source_id, chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS post_versions (
                id INTEGER PRIMARY KEY,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                UNIQUE(post_id, text)
            );

            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                mime_type TEXT,
                local_path TEXT,
                telegram_file_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(post_id, telegram_file_id)
            );

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('active', 'expired')),
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                location_label TEXT,
                location_precision TEXT NOT NULL,
                latitude REAL,
                longitude REAL,
                radius_m INTEGER,
                first_reported_at TEXT NOT NULL,
                last_confirmed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_posts (
                event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('initial', 'update', 'comment')),
                created_at TEXT NOT NULL,
                PRIMARY KEY(event_id, post_id)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                event_id TEXT REFERENCES events(id),
                action TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS geocode_cache (
                query TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS posts_pending_idx ON posts(llm_status, posted_at);
            CREATE INDEX IF NOT EXISTS events_active_idx ON events(status, expires_at);
            CREATE INDEX IF NOT EXISTS event_posts_post_idx ON event_posts(post_id);
            """
        )
        for table, additions in {
            "posts": {
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "content_hash": "TEXT",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "TEXT",
                "last_error": "TEXT",
            },
            "events": {
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "location_query": "TEXT",
                "location_provider": "TEXT",
                "location_confidence": "REAL",
            },
            "event_posts": {
                "accepted_text": "TEXT",
                "accepted_raw_json": "TEXT",
                "accepted_revision": "INTEGER",
            },
        }.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in additions.items():
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS worker_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence_revisions (
                post_id INTEGER NOT NULL REFERENCES posts(id), revision INTEGER NOT NULL,
                text TEXT NOT NULL, raw_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
                PRIMARY KEY(post_id, revision));
            CREATE VIRTUAL TABLE IF NOT EXISTS event_search USING fts5(event_id UNINDEXED, content);
        """)
        conn.execute(
            "UPDATE event_posts SET accepted_text=(SELECT text FROM posts WHERE id=post_id), accepted_raw_json=(SELECT raw_json FROM posts WHERE id=post_id), accepted_revision=(SELECT revision FROM posts WHERE id=post_id) WHERE accepted_text IS NULL"
        )
        conn.execute(
            "INSERT OR IGNORE INTO evidence_revisions SELECT id,revision,text,raw_json,posted_at FROM posts"
        )
        conn.execute("DELETE FROM event_search")
        conn.execute(
            "INSERT INTO event_search SELECT id,title || ' ' || description || ' ' || COALESCE(location_label,'') FROM events"
        )
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    finally:
        conn.close()


def ensure_source(source: dict[str, str], path: Path | str = DB_PATH) -> int:
    conn = connect(path)
    try:
        conn.execute(
            """
            INSERT INTO sources(source_key, name, username, timezone, created_at)
            VALUES (:key, :name, :username, :timezone, :created_at)
            ON CONFLICT(source_key) DO UPDATE SET
                name = excluded.name, username = excluded.username, timezone = excluded.timezone
            """,
            {**source, "created_at": now()},
        )
        row = conn.execute(
            "SELECT id FROM sources WHERE source_key = ?", (source["key"],)
        ).fetchone()
        conn.commit()
        return int(row["id"])
    finally:
        conn.close()


def _fingerprint(text: str, raw: str) -> str:
    return hashlib.sha256((text + "\0" + raw).encode()).hexdigest()


def _safe_link(link: str | None) -> str | None:
    parsed = urlparse(link or "")
    return (
        link
        if parsed.scheme == "https" and parsed.netloc == "t.me" and not parsed.username
        else None
    )


def _bump(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute(
        "UPDATE events SET revision=revision+1,updated_at=? WHERE id IN (SELECT event_id FROM event_posts WHERE post_id=?)",
        (now(), post_id),
    )


def save_post(post: dict[str, Any], path: Path | str = DB_PATH) -> tuple[int, bool]:
    raw = post.get("raw_json", {})
    if isinstance(raw, str):
        raw = json.loads(raw)
    raw = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    text = post.get("text", "")
    fingerprint = _fingerprint(text, raw)
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT * FROM posts WHERE source_id=? AND chat_id=? AND message_id=?",
            (post["source_id"], post["chat_id"], post["message_id"]),
        ).fetchone()
        if previous:
            post_id = previous["id"]
            incoming_time = post.get("edited_at") or post["posted_at"]
            previous_time = previous["edited_at"] or previous["posted_at"]
            if datetime.fromisoformat(incoming_time) < datetime.fromisoformat(
                previous_time
            ):
                return post_id, False
            changed = (
                previous["content_hash"]
                or _fingerprint(
                    previous["text"],
                    json.dumps(
                        json.loads(previous["raw_json"]),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            ) != fingerprint
            if not changed:
                return post_id, False
            revision = previous["revision"] + 1
            parent = post.get("parent_post_id") or previous["parent_post_id"]
            conn.execute(
                "UPDATE posts SET text=?,raw_json=?,edited_at=?,revision=?,content_hash=?,parent_post_id=?,link=COALESCE(?,link),llm_status=?,attempts=0,next_attempt_at=NULL,last_error=NULL WHERE id=?",
                (
                    text,
                    raw,
                    post.get("edited_at"),
                    revision,
                    fingerprint,
                    parent,
                    _safe_link(post.get("link")),
                    "evidence" if parent else "pending",
                    post_id,
                ),
            )
        else:
            revision = 1
            parent = post.get("parent_post_id")
            cursor = conn.execute(
                "INSERT INTO posts(source_id,chat_id,message_id,parent_post_id,posted_at,edited_at,text,original_text,link,raw_json,llm_status,revision,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    post["source_id"],
                    post["chat_id"],
                    post["message_id"],
                    parent,
                    post["posted_at"],
                    post.get("edited_at"),
                    text,
                    text,
                    _safe_link(post.get("link")),
                    raw,
                    "evidence" if parent else "pending",
                    revision,
                    fingerprint,
                ),
            )
            post_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO evidence_revisions VALUES (?,?,?,?,?)",
            (post_id, revision, text, raw, now()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO post_versions(post_id,text,raw_json,recorded_at) VALUES (?,?,?,?)",
            (post_id, text, raw, now()),
        )
        if parent:
            conn.execute(
                "UPDATE event_posts SET accepted_text=?,accepted_raw_json=?,accepted_revision=? WHERE post_id=? AND role='comment'",
                (text, raw, revision, post_id),
            )
            _bump(conn, post_id)
        return int(post_id), True


def save_media(post_id: int, item: dict[str, Any], path: Path | str = DB_PATH) -> int:
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM media WHERE post_id=? AND telegram_file_id IS ?",
            (post_id, item.get("telegram_file_id")),
        ).fetchone()
        if row:
            if item.get("local_path") and row["local_path"] != item["local_path"]:
                conn.execute(
                    "UPDATE media SET local_path=? WHERE id=?",
                    (item["local_path"], row["id"]),
                )
                _bump(conn, post_id)
            return row["id"]
        cursor = conn.execute(
            "INSERT INTO media(post_id,kind,mime_type,local_path,telegram_file_id,created_at) VALUES (?,?,?,?,?,?)",
            (
                post_id,
                item["kind"],
                item.get("mime_type"),
                item.get("local_path"),
                item.get("telegram_file_id"),
                now(),
            ),
        )
        _bump(conn, post_id)
        return int(cursor.lastrowid)


def set_media_path(
    post_id: int, telegram_file_id: str, local_path: str, path: Path | str = DB_PATH
) -> None:
    with connect(path) as conn:
        cursor = conn.execute(
            "UPDATE media SET local_path=? WHERE post_id=? AND telegram_file_id=? AND local_path IS NOT ?",
            (local_path, post_id, telegram_file_id, local_path),
        )
        if cursor.rowcount:
            _bump(conn, post_id)


def get_post(post_id: int, path: Path | str = DB_PATH) -> dict[str, Any] | None:
    with connect(path) as conn:
        row = conn.execute(
            "SELECT p.*,s.source_key,s.name AS source_name,s.username,s.timezone FROM posts p JOIN sources s ON s.id=p.source_id WHERE p.id=?",
            (post_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["has_media"] = bool(json.loads(result["raw_json"]).get("has_media"))
        return result


def event_for_post(post_id: int, path: Path | str = DB_PATH) -> str | None:
    with connect(path) as conn:
        row = conn.execute(
            "SELECT event_id FROM event_posts WHERE post_id=? AND role<>'comment' LIMIT 1",
            (post_id,),
        ).fetchone()
        return row[0] if row else None


def active_briefing(timezone: str, path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    return list_events(path)[:30]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a = (
        math.sin(math.radians(lat2 - lat1) / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0, 1 - a)))


def find_candidates(
    text: str,
    latitude: float | str | None = None,
    longitude: float | str | None = None,
    hours: int = 72,
    path: Path | str = DB_PATH,
) -> list[dict[str, Any]]:
    since = (datetime.now(UTC) - timedelta(hours=min(max(hours, 1), 168))).isoformat()
    tokens = re.findall(r"[^\W_]{3,}", text.lower(), re.UNICODE)[:12]
    query = " OR ".join('"' + word + '"*' for word in tokens)
    with connect(path) as conn:
        rows = (
            conn.execute(
                "SELECT e.* FROM event_search JOIN events e ON e.id=event_search.event_id WHERE event_search MATCH ? AND e.status='active' AND e.expires_at>? AND e.last_confirmed_at>=? ORDER BY rank LIMIT 8",
                (query, now(), since),
            ).fetchall()
            if query
            else []
        )
    results = [dict(row) for row in rows]
    for item in results:
        item["distance_m"] = None
        try:
            if (
                latitude is not None
                and longitude is not None
                and item["latitude"] is not None
            ):
                item["distance_m"] = round(
                    haversine_m(
                        float(latitude),
                        float(longitude),
                        item["latitude"],
                        item["longitude"],
                    )
                )
        except (ValueError, TypeError):
            pass
    return results


def apply_decision(
    post_id: int,
    decision: dict[str, Any],
    path: Path | str = DB_PATH,
    expected_revision: int | None = None,
) -> str | None:
    action = decision["action"]
    if action not in {"ignore", "create", "append"}:
        raise ValueError("Unsupported decision action")
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not post:
            raise ValueError("Unknown post")
        if expected_revision is not None and post["revision"] != expected_revision:
            return None
        associated = conn.execute(
            "SELECT event_id FROM event_posts WHERE post_id=? AND role<>'comment' LIMIT 1",
            (post_id,),
        ).fetchone()
        event_id = (
            associated[0]
            if associated
            else decision.get("event_id")
            if action == "append"
            else None
        )
        if action == "ignore":
            conn.execute(
                "UPDATE posts SET llm_status='ignored',next_attempt_at=NULL,last_error=NULL WHERE id=?",
                (post_id,),
            )
            if not associated:
                conn.execute(
                    "UPDATE posts SET text='',original_text='',raw_json='{}' WHERE id=?",
                    (post_id,),
                )
                conn.execute("DELETE FROM post_versions WHERE post_id=?", (post_id,))
                conn.execute(
                    "DELETE FROM evidence_revisions WHERE post_id=?", (post_id,)
                )
            conn.execute(
                "INSERT INTO decisions(post_id,event_id,action,result_json,created_at) VALUES (?,?,?,?,?)",
                (
                    post_id,
                    event_id,
                    action,
                    json.dumps(decision, ensure_ascii=False),
                    now(),
                ),
            )
            return None
        severity = decision["severity"]
        if severity not in _MIN_DISPLAY_MINUTES:
            raise ValueError("Invalid severity")
        precision = decision.get("location_precision", "unknown")
        lat, lon = decision.get("latitude"), decision.get("longitude")
        if (lat is None) != (lon is None) or (
            lat is not None
            and (
                not math.isfinite(float(lat))
                or not math.isfinite(float(lon))
                or not -90 <= float(lat) <= 90
                or not -180 <= float(lon) <= 180
            )
        ):
            raise ValueError("Invalid coordinates")
        for field in ("kind", "title", "description"):
            if not isinstance(decision.get(field), str) or not decision[field].strip():
                raise ValueError("Missing " + field)
        existing = (
            conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if event_id
            else None
        )
        if action == "append" and not existing:
            raise ValueError("append must reference an existing event_id")
        stamp = now()
        confirmation = post["edited_at"] or post["posted_at"]
        expiry = (
            datetime.fromisoformat(confirmation)
            + timedelta(
                minutes=effective_display_minutes(
                    severity, decision.get("display_minutes", 360)
                )
            )
        ).isoformat()
        if existing:
            confirmed = existing["last_confirmed_at"]
            expires = existing["expires_at"]
            if decision.get("extends_lifetime", False):
                confirmed = max(confirmed, confirmation)
                expires = max(expires, expiry)
            location_fields = (
                "location_label",
                "location_precision",
                "latitude",
                "longitude",
                "radius_m",
                "location_query",
                "location_provider",
                "location_confidence",
            )
            location = {field: existing[field] for field in location_fields}
            label, query = (
                decision.get("location_label"),
                decision.get("location_query"),
            )
            if query and existing["location_query"]:
                incoming, accepted = query, existing["location_query"]
            elif label and existing["location_label"]:
                incoming, accepted = label, existing["location_label"]
            else:
                incoming, accepted = (
                    query or label,
                    existing["location_query"] or existing["location_label"],
                )

            def normalized(value):
                return " ".join(re.findall(r"\w+", (value or "").casefold()))

            changed_place = bool(incoming) and normalized(incoming) != normalized(
                accepted
            )
            if decision.get("reset_location"):
                location.update({key: None for key in location_fields})
                location.update(
                    location_label=label or query,
                    location_query=query,
                    location_precision=precision,
                )
            elif lat is None and changed_place:
                # A corrected but unresolved place cannot inherit the previous place's map point.
                location.update(
                    location_label=label or query,
                    location_query=query,
                    location_precision="unknown",
                    latitude=None,
                    longitude=None,
                    radius_m=None,
                    location_provider=None,
                    location_confidence=None,
                )
            else:
                for field in location_fields:
                    value = decision.get(field)
                    if field == "location_provider":
                        value = decision.get(field, decision.get("location_source"))
                    if value is not None and (
                        lat is not None
                        or field
                        not in {
                            "location_precision",
                            "location_provider",
                            "location_confidence",
                        }
                    ):
                        location[field] = value
            conn.execute(
                "UPDATE events SET kind=?,severity=?,title=?,description=?,location_label=?,location_precision=?,latitude=?,longitude=?,radius_m=?,location_query=?,location_provider=?,location_confidence=?,last_confirmed_at=?,expires_at=?,status=?,updated_at=?,revision=revision+1 WHERE id=?",
                (
                    decision["kind"],
                    severity,
                    decision["title"],
                    decision["description"],
                    *(location[field] for field in location_fields),
                    confirmed,
                    expires,
                    "active" if expires > stamp else "expired",
                    stamp,
                    event_id,
                ),
            )
            role = "update"
        else:
            event_id = str(uuid.uuid4())
            role = "initial"
            conn.execute(
                "INSERT INTO events(id,status,kind,severity,title,description,location_label,location_precision,latitude,longitude,radius_m,first_reported_at,last_confirmed_at,expires_at,created_at,updated_at,location_query,location_provider,location_confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    "active" if expiry > stamp else "expired",
                    decision["kind"],
                    severity,
                    decision["title"],
                    decision["description"],
                    decision.get("location_label"),
                    precision,
                    lat,
                    lon,
                    decision.get("radius_m"),
                    post["posted_at"],
                    confirmation,
                    expiry,
                    stamp,
                    stamp,
                    decision.get("location_query"),
                    decision.get("location_provider", decision.get("location_source")),
                    decision.get("location_confidence"),
                ),
            )
        conn.execute(
            "INSERT INTO event_posts(event_id,post_id,role,created_at,accepted_text,accepted_raw_json,accepted_revision) VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id,post_id) DO UPDATE SET accepted_text=excluded.accepted_text,accepted_raw_json=excluded.accepted_raw_json,accepted_revision=excluded.accepted_revision",
            (
                event_id,
                post_id,
                role,
                stamp,
                post["text"],
                post["raw_json"],
                post["revision"],
            ),
        )
        conn.execute(
            "UPDATE posts SET llm_status='done',attempts=0,next_attempt_at=NULL,last_error=NULL WHERE id=?",
            (post_id,),
        )
        conn.execute(
            "INSERT INTO decisions(post_id,event_id,action,result_json,created_at) VALUES (?,?,?,?,?)",
            (
                post_id,
                event_id,
                action,
                json.dumps(decision, ensure_ascii=False),
                stamp,
            ),
        )
        conn.execute("DELETE FROM event_search WHERE event_id=?", (event_id,))
        conn.execute(
            "INSERT INTO event_search SELECT id,title || ' ' || description || ' ' || COALESCE(location_label,'') FROM events WHERE id=?",
            (event_id,),
        )
        return event_id


def attach_comment(
    parent_post_id: int, comment_post_id: int, path: Path | str = DB_PATH
) -> None:
    with connect(path) as conn:
        post = conn.execute(
            "SELECT * FROM posts WHERE id=?", (comment_post_id,)
        ).fetchone()
        if not post:
            raise ValueError("Unknown comment")
        conn.execute(
            "UPDATE posts SET llm_status='evidence',parent_post_id=? WHERE id=?",
            (parent_post_id, comment_post_id),
        )
        for row in conn.execute(
            "SELECT event_id FROM event_posts WHERE post_id=?", (parent_post_id,)
        ).fetchall():
            cursor = conn.execute(
                "INSERT INTO event_posts(event_id,post_id,role,created_at,accepted_text,accepted_raw_json,accepted_revision) VALUES (?,?,'comment',?,?,?,?) ON CONFLICT(event_id,post_id) DO UPDATE SET accepted_text=excluded.accepted_text,accepted_raw_json=excluded.accepted_raw_json,accepted_revision=excluded.accepted_revision WHERE event_posts.accepted_text IS NOT excluded.accepted_text OR event_posts.accepted_raw_json IS NOT excluded.accepted_raw_json OR event_posts.accepted_revision IS NOT excluded.accepted_revision",
                (
                    row[0],
                    comment_post_id,
                    now(),
                    post["text"],
                    post["raw_json"],
                    post["revision"],
                ),
            )
            if cursor.rowcount:
                conn.execute(
                    "UPDATE events SET revision=revision+1,updated_at=? WHERE id=?",
                    (now(), row[0]),
                )


def cleanup_expired_media(path: Path | str = DB_PATH) -> int:
    media_dir = settings.media.resolve()
    cleaned = 0
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT m.id,m.local_path FROM media m WHERE m.local_path IS NOT NULL AND EXISTS (SELECT 1 FROM event_posts ep JOIN events e ON e.id=ep.event_id WHERE ep.post_id=m.post_id AND e.status='expired') AND NOT EXISTS (SELECT 1 FROM event_posts ep JOIN events e ON e.id=ep.event_id WHERE ep.post_id=m.post_id AND e.status='active' AND e.expires_at>?)",
            (now(),),
        ).fetchall()
        for row in rows:
            candidate = (media_dir / row["local_path"]).resolve()
            if candidate.is_relative_to(media_dir) and candidate != media_dir:
                candidate.unlink(missing_ok=True)
                conn.execute(
                    "UPDATE media SET local_path=NULL WHERE id=?", (row["id"],)
                )
                cleaned += 1
    return cleaned


def expire_events(path: Path | str = DB_PATH) -> int:
    with connect(path) as conn:
        count = conn.execute(
            "UPDATE events SET status='expired',revision=revision+1,updated_at=? WHERE status='active' AND expires_at<=?",
            (now(), now()),
        ).rowcount
    cleanup_expired_media(path)
    return count


def purge_expired_events(path: Path | str = DB_PATH) -> tuple[int, int]:
    return expire_events(path), 0


def list_events(path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT e.*,COUNT(ep.post_id) AS updates,COUNT(ep.post_id) AS message_count,COUNT(DISTINCT CASE WHEN ep.role<>'comment' THEN p.source_id END) AS source_count,(SELECT s.name FROM event_posts ep2 JOIN posts p2 ON p2.id=ep2.post_id JOIN sources s ON s.id=p2.source_id WHERE ep2.event_id=e.id AND ep2.role<>'comment' ORDER BY p2.posted_at DESC LIMIT 1) AS source_name FROM events e LEFT JOIN event_posts ep ON ep.event_id=e.id LEFT JOIN posts p ON p.id=ep.post_id WHERE e.status='active' AND e.expires_at>? GROUP BY e.id ORDER BY e.last_confirmed_at DESC",
            (now(),),
        ).fetchall()
        results = [dict(row) for row in rows]
        for event in results:
            event["source_names"] = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT s.name FROM event_posts ep JOIN posts p ON p.id=ep.post_id JOIN sources s ON s.id=p.source_id WHERE ep.event_id=? AND ep.role<>'comment' ORDER BY s.name",
                    (event["id"],),
                )
            ]
            event["location_source"] = event["location_provider"]
            _hide_unlocated_coordinates(event)
        return results


def get_event(
    event_id: str, path: Path | str = DB_PATH, include_expired: bool = False
) -> dict[str, Any] | None:
    with connect(path) as conn:
        event = conn.execute(
            "SELECT * FROM events WHERE id=?"
            + ("" if include_expired else " AND status='active' AND expires_at>?"),
            (event_id,) if include_expired else (event_id, now()),
        ).fetchone()
        if not event:
            return None
        result = dict(event)
        result["feed"] = []
        rows = conn.execute(
            "SELECT ep.role,p.id,COALESCE(ep.accepted_text,p.text) AS text,p.posted_at,p.edited_at,p.link,p.deleted_at,p.source_id,s.source_key,s.name AS source_name,s.username,s.timezone,COALESCE(ep.accepted_revision,p.revision) AS revision FROM event_posts ep JOIN posts p ON p.id=ep.post_id JOIN sources s ON s.id=p.source_id WHERE ep.event_id=? ORDER BY p.posted_at,p.id",
            (event_id,),
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["link"] = _safe_link(item["link"])
            item["media"] = []
            for media in conn.execute(
                "SELECT kind,mime_type,local_path FROM media WHERE post_id=? AND local_path IS NOT NULL ORDER BY id",
                (item["id"],),
            ):
                relative = Path(media["local_path"])
                if not relative.is_absolute() and ".." not in relative.parts:
                    item["media"].append(
                        {
                            "url": "/media/" + quote(str(relative), safe="/"),
                            "mime_type": media["mime_type"]
                            or "application/octet-stream",
                            "kind": media["kind"],
                        }
                    )
            item["revisions"] = [
                dict(version)
                for version in conn.execute(
                    "SELECT revision,text,recorded_at FROM evidence_revisions WHERE post_id=? AND revision<=? ORDER BY revision",
                    (item["id"], item["revision"]),
                )
            ]
            result["feed"].append(item)
        result["message_count"] = len(rows)
        result["updates"] = len(rows)
        result["source_names"] = sorted(
            {row["source_name"] for row in rows if row["role"] != "comment"}
        )
        result["location_source"] = result["location_provider"]
        _hide_unlocated_coordinates(result)
        result["source_count"] = len(
            {row["source_id"] for row in rows if row["role"] != "comment"}
        )
        return result


def _hide_unlocated_coordinates(event: dict) -> None:
    # Older city-centre pins are not incident locations; keep their source history intact.
    if event["location_precision"] in {"city", "unknown"}:
        for key in (
            "latitude",
            "longitude",
            "radius_m",
            "location_confidence",
            "location_source",
        ):
            event[key] = None


def stream_revision(path: Path | str = DB_PATH) -> str:
    # A complete visible payload also captures source renames and expiry without writes.
    events = list_events(path)
    payload = [get_event(event["id"], path) for event in events]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def pending_post_ids(path: Path | str = DB_PATH, hours: int = 168) -> list[int]:
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    with connect(path) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT id FROM posts WHERE llm_status IN ('pending','blocked') AND parent_post_id IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?) AND (COALESCE(edited_at,posted_at)>=? OR EXISTS (SELECT 1 FROM event_posts WHERE post_id=posts.id)) ORDER BY COALESCE(edited_at,posted_at) DESC",
                (now(), since),
            )
        ]


def schedule_retry(
    post_id: int, error: Any, blocked: bool = False, path: Path | str = DB_PATH
) -> None:
    # Store an error class, never provider response bodies that may contain credentials.
    code = type(error).__name__ if isinstance(error, BaseException) else "ProviderError"
    with connect(path) as conn:
        row = conn.execute(
            "SELECT attempts FROM posts WHERE id=?", (post_id,)
        ).fetchone()
        if not row:
            return
        attempts = row[0] + 1
        audit = getattr(error, "audit", None)
        if audit:
            conn.execute(
                "INSERT INTO decisions(post_id,event_id,action,result_json,created_at) VALUES (?,NULL,'defer',?,?)",
                (
                    post_id,
                    json.dumps({"audit": audit, "error": code}, ensure_ascii=False),
                    now(),
                ),
            )
        try:
            retry_after = float(getattr(error, "retry_after", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            retry_after = 0
        if not math.isfinite(retry_after):
            retry_after = 0
        delay = min(86400, max(min(3600, 15 * 2 ** min(attempts, 8)), retry_after))
        retry = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        conn.execute(
            "UPDATE posts SET attempts=?,next_attempt_at=?,last_error=?,llm_status=? WHERE id=?",
            (attempts, retry, code, "blocked" if blocked else "pending", post_id),
        )


def worker_status(key: str, value: Any, path: Path | str = DB_PATH) -> None:
    if key not in {
        "heartbeat",
        "started_at",
        "backfill_completed_at",
        "last_polled_at",
        "last_success",
        "state",
        "last_error",
        "provider_blocked",
        "latency_ms",
        "cost_usd",
        "model",
    }:
        raise ValueError("Unknown worker status key")
    if key == "last_error":
        value = (
            type(value).__name__
            if isinstance(value, BaseException)
            else "ProviderError"
            if value
            else None
        )
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO worker_state VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, json.dumps(value), now()),
        )


def status_summary(path: Path | str = DB_PATH) -> dict[str, Any]:
    with connect(path) as conn:
        result = {
            row["key"]: json.loads(row["value"])
            for row in conn.execute("SELECT * FROM worker_state")
        }
        since = (datetime.now(UTC) - timedelta(hours=168)).isoformat()
        result["pending"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE llm_status IN ('pending','blocked') AND parent_post_id IS NULL AND (COALESCE(edited_at,posted_at)>=? OR EXISTS (SELECT 1 FROM event_posts WHERE post_id=posts.id))",
            (since,),
        ).fetchone()[0]
        result["stale_pending"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE llm_status IN ('pending','blocked') AND parent_post_id IS NULL AND COALESCE(edited_at,posted_at)<? AND NOT EXISTS (SELECT 1 FROM event_posts WHERE post_id=posts.id)",
            (since,),
        ).fetchone()[0]
        result["blocked"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE llm_status='blocked'"
        ).fetchone()[0]
        result["errors"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE last_error IS NOT NULL"
        ).fetchone()[0]
        result.setdefault("heartbeat", None)
        result.setdefault("last_success", None)
        return result


def discard_non_source_posts(
    source_chat_ids: dict[str, int], path: Path | str = DB_PATH
) -> tuple[int, int]:
    with connect(path) as conn:
        count = 0
        for source_key, chat_id in source_chat_ids.items():
            count += conn.execute(
                "UPDATE posts SET llm_status='evidence' WHERE source_id=(SELECT id FROM sources WHERE source_key=?) AND parent_post_id IS NULL AND chat_id<>? AND llm_status='pending'",
                (source_key, chat_id),
            ).rowcount
        return count, 0


def event_source_posts_for_comments(path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    with connect(path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT DISTINCT p.id,p.message_id,p.chat_id,s.username FROM event_posts ep JOIN posts p ON p.id=ep.post_id JOIN sources s ON s.id=p.source_id JOIN events e ON e.id=ep.event_id WHERE ep.role IN ('initial','update') AND e.status='active' AND e.expires_at>?",
                (now(),),
            )
        ]

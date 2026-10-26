import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import storage
import web


class WebTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "db.sqlite3"
        self.media = Path(temp.name) / "media"
        self.media.mkdir()
        storage.init_db(self.db)
        source = storage.ensure_source(
            {
                "key": "test",
                "name": "Тест",
                "username": "testsource",
                "timezone": "Europe/Moscow",
            },
            self.db,
        )
        self.pid, _ = storage.save_post(
            {
                "source_id": source,
                "chat_id": 1,
                "message_id": 1,
                "posted_at": datetime.now(UTC).isoformat(),
                "text": "Сообщается о пожаре",
                "raw_json": {},
                "link": "https://t.me/testsource/1",
            },
            self.db,
        )
        self.eid = storage.apply_decision(
            self.pid,
            {
                "action": "create",
                "kind": "fire",
                "severity": "danger",
                "title": "Пожар",
                "description": "Источник сообщает о пожаре",
                "location_precision": "unknown",
                "display_minutes": 1440,
            },
            self.db,
        )
        for name in [
            "connect",
            "init_db",
            "status_summary",
            "stream_revision",
            "feed_snapshot",
        ]:
            original = getattr(storage, name)
            patcher = patch.object(
                web,
                name,
                side_effect=lambda *args, _fn=original, **kw: _fn(
                    *args, path=self.db, **kw
                ),
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(
            web, "get_event", side_effect=lambda eid: storage.get_event(eid, self.db)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(
            web, "settings", replace(web.settings, database=self.db, media=self.media)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.enterContext(TestClient(web.app))

    def test_public_schema_and_security_headers(self):
        response = self.client.get("/api/events")
        self.assertEqual(response.status_code, 200)
        event = web.EventBrief.model_validate(response.json()[0])
        self.assertEqual(event.id, self.eid)
        detail = self.client.get("/api/events/" + self.eid)
        parsed = web.EventDetail.model_validate(detail.json())
        self.assertEqual(parsed.feed[0].id, self.pid)
        self.assertEqual(parsed.message_count, 1)
        self.assertEqual(parsed.source_count, 1)
        for header, value in [
            ("x-content-type-options", "nosniff"),
            ("x-frame-options", "DENY"),
            ("cache-control", "private, no-cache"),
        ]:
            self.assertEqual(response.headers[header], value)
        self.assertIn("object-src 'none'", response.headers["content-security-policy"])
        config = self.client.get("/api/config").json()
        self.assertIn("timezone", config)
        self.assertNotIn("api_key", config)
        self.assertEqual(self.client.get("/healthz").json()["status"], "ok")

    def test_expired_reads_do_not_modify_evidence_or_delete_media(self):
        (self.media / "photo.jpg").write_bytes(b"photo")
        storage.save_media(
            self.pid,
            {
                "telegram_file_id": "1",
                "kind": "photo",
                "mime_type": "image/jpeg",
                "local_path": "photo.jpg",
            },
            self.db,
        )
        with storage.connect(self.db) as conn:
            conn.execute(
                "UPDATE events SET expires_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), self.eid),
            )
            before = [tuple(row) for row in conn.execute("SELECT * FROM event_posts")]
        self.assertEqual(self.client.get("/api/events").json(), [])
        self.assertEqual(self.client.get("/api/events/" + self.eid).status_code, 404)
        self.assertEqual(self.client.get("/media/photo.jpg").status_code, 404)
        self.client.get("/api/status")
        with storage.connect(self.db) as conn:
            self.assertEqual(
                before,
                [tuple(row) for row in conn.execute("SELECT * FROM event_posts")],
            )
            self.assertEqual(
                conn.execute("SELECT status FROM events").fetchone()[0], "active"
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM media").fetchone()[0], 1
            )
        self.assertTrue((self.media / "photo.jpg").exists())

    def test_media_requires_active_association_and_forces_unsafe_download(self):
        for name, mime in [("photo.jpg", "image/jpeg"), ("payload.html", "text/html")]:
            (self.media / name).write_bytes(b"test")
            storage.save_media(
                self.pid,
                {
                    "telegram_file_id": name,
                    "kind": "document",
                    "mime_type": mime,
                    "local_path": name,
                },
                self.db,
            )
        photo = self.client.get("/media/photo.jpg")
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.headers["content-type"], "image/jpeg")
        unsafe = self.client.get("/media/payload.html")
        self.assertEqual(unsafe.status_code, 200)
        self.assertIn("attachment", unsafe.headers["content-disposition"])
        self.assertEqual(unsafe.headers["content-type"], "application/octet-stream")
        (self.media / "unattached.jpg").write_bytes(b"not accepted")
        self.assertEqual(self.client.get("/media/unattached.jpg").status_code, 404)
        self.assertEqual(self.client.get("/media/%2e%2e%2fdb.sqlite3").status_code, 404)
        (self.media / "outside.jpg").symlink_to(self.db)
        self.assertEqual(self.client.get("/media/outside.jpg").status_code, 404)

    def test_status_heartbeat_and_blocked_state_are_truthful(self):
        self.assertFalse(self.client.get("/api/status").json()["connected"])
        storage.worker_status("heartbeat", datetime.now(UTC).isoformat(), self.db)
        storage.worker_status("state", "watching", self.db)
        storage.worker_status("provider_blocked", True, self.db)
        status = self.client.get("/api/status").json()
        self.assertTrue(status["connected"])
        self.assertTrue(status["provider_blocked"])

    def test_sse_initial_revision_and_disconnect_without_hanging(self):
        async def exercise():
            request = AsyncMock()
            request.is_disconnected.side_effect = [False, True]
            hub = web.UpdateHub("revision-7")
            with patch.object(web, "update_hub", hub), patch.object(hub, "wait", new=AsyncMock()):
                response = await web.stream(request)
                chunks = [chunk async for chunk in response.body_iterator]
            self.assertEqual(chunks, ["event: events\ndata: revision-7\n\n"])
            self.assertEqual(response.headers["x-accel-buffering"], "no")

        asyncio.run(exercise())

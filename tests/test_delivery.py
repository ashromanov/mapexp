"""Delivery regressions: cache consistency, bounded SQL work, and shared SSE."""

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import test_storage as storage_fixtures
import test_web as web_fixtures

import storage
import web


# Reuse the existing isolated fixture without inheriting and rerunning its tests.
class RevisionTests(unittest.TestCase):
    setUp = storage_fixtures.StorageTests.setUp
    post = storage_fixtures.StorageTests.post
    decision = storage_fixtures.StorageTests.decision
    event = storage_fixtures.StorageTests.event

    def test_revision_survives_restart_and_ignores_noop_and_worker_heartbeat(self):
        _, eid = self.event()
        token = storage.stream_revision(self.db)
        storage.init_db(self.db)
        storage.worker_status("heartbeat", storage.now(), self.db)
        with storage.connect(self.db) as conn:
            conn.execute("UPDATE events SET title=title WHERE id=?", (eid,))
        self.assertEqual(storage.stream_revision(self.db), token)
        with storage.connect(self.db) as conn:
            conn.execute(
                "UPDATE sources SET name='Новое имя' WHERE id=?", (self.source,)
            )
        self.assertNotEqual(storage.stream_revision(self.db), token)
        self.assertEqual(storage.list_events(self.db)[0]["source_names"], ["Новое имя"])

    def test_expiry_changes_revision_without_writes(self):
        _, eid = self.event()
        expiry = self.time + timedelta(hours=1)
        with storage.connect(self.db) as conn:
            conn.execute(
                "UPDATE events SET expires_at=? WHERE id=?", (expiry.isoformat(), eid)
            )
        with patch.object(
            storage, "now", return_value=(expiry - timedelta(seconds=1)).isoformat()
        ):
            before = storage.stream_revision(self.db)
            self.assertEqual(len(storage.list_events(self.db)), 1)
        with patch.object(storage, "now", return_value=expiry.isoformat()):
            self.assertNotEqual(storage.stream_revision(self.db), before)
            self.assertEqual(storage.list_events(self.db), [])
        with storage.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM events").fetchone()[0], "active"
            )

    def test_rollback_does_not_change_revision(self):
        _, eid = self.event()
        token = storage.stream_revision(self.db)
        with storage.connect(self.db) as conn:
            conn.execute("UPDATE events SET title='Rolled back' WHERE id=?", (eid,))
            conn.rollback()
        self.assertEqual(storage.stream_revision(self.db), token)

    def test_feed_snapshot_is_atomic_during_concurrent_write(self):
        _, eid = self.event()
        token = storage.stream_revision(self.db)
        original = storage._list_events

        def write_between_token_and_rows(conn, stamp):
            with storage.connect(self.db) as writer:
                writer.execute("UPDATE events SET title='New title' WHERE id=?", (eid,))
            return original(conn, stamp)

        with patch.object(
            storage, "_list_events", side_effect=write_between_token_and_rows
        ):
            revision, items = storage.feed_snapshot(self.db)
        self.assertEqual(revision, token)
        self.assertNotEqual(items[0]["title"], "New title")
        revision, items = storage.feed_snapshot(self.db)
        self.assertNotEqual(revision, token)
        self.assertEqual(items[0]["title"], "New title")

    def test_query_count_does_not_grow_with_event_count(self):
        for i in range(20):
            pid, _ = storage.save_post(self.post(message=i + 1), self.db)
            storage.apply_decision(pid, self.decision(), self.db)
        calls = []
        original = storage.connect

        def trace(*args, **kw):
            conn = original(*args, **kw)
            conn.set_trace_callback(calls.append)
            return conn

        with patch.object(storage, "connect", side_effect=trace):
            self.assertEqual(len(storage.list_events(self.db)), 20)
            self.assertEqual(
                sum(s.lstrip().upper().startswith("SELECT") for s in calls), 2
            )
            calls.clear()
            storage.stream_revision(self.db)
            self.assertEqual(
                sum(s.lstrip().upper().startswith("SELECT") for s in calls), 1
            )


class CacheTests(unittest.TestCase):
    setUp = web_fixtures.WebTests.setUp

    def test_cache_singleflight_and_invalidates_after_write(self):
        cache = web.FeedCache()
        with patch.object(web, "feed_snapshot", wraps=web.feed_snapshot) as build:
            with ThreadPoolExecutor(max_workers=20) as pool:
                snapshots = list(pool.map(lambda _: cache.refresh(), range(40)))
            self.assertEqual(build.call_count, 1)
            self.assertTrue(all(item is snapshots[0] for item in snapshots))
            with storage.connect(self.db) as conn:
                conn.execute(
                    "UPDATE events SET title='Changed' WHERE id=?", (self.eid,)
                )
            changed = cache.refresh()
            self.assertEqual(build.call_count, 2)
        self.assertNotEqual(changed.etag, snapshots[0].etag)
        self.assertIn(b"Changed", changed.body)

    def test_conditional_requests_and_compression_variants(self):
        plain = self.client.get("/api/events", headers={"Accept-Encoding": "identity"})
        zipped = self.client.get("/api/events", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(plain.content, zipped.content)  # HTTP client decompresses.
        self.assertEqual(zipped.headers["content-encoding"], "gzip")
        self.assertNotEqual(plain.headers["etag"], zipped.headers["etag"])
        for match in [
            plain.headers["etag"],
            "W/" + plain.headers["etag"],
            "*",
            '"other", ' + plain.headers["etag"],
        ]:
            response = self.client.get(
                "/api/events",
                headers={"Accept-Encoding": "identity", "If-None-Match": match},
            )
            self.assertEqual(response.status_code, 304)
            self.assertEqual(response.content, b"")
            self.assertEqual(response.headers["etag"], plain.headers["etag"])
            self.assertEqual(response.headers["vary"], "Accept-Encoding")
            self.assertEqual(response.headers["cache-control"], "private, no-cache")
        # A strong tag for a different representation cannot produce a 304.
        response = self.client.get(
            "/api/events",
            headers={
                "Accept-Encoding": "identity",
                "If-None-Match": zipped.headers["etag"],
            },
        )
        self.assertEqual(response.status_code, 200)
        for encoding in ["gzip;q=0", "gzip;q=0, *;q=1", "gzip;q=garbage", "br"]:
            response = self.client.get(
                "/api/events", headers={"Accept-Encoding": encoding}
            )
            self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(
            self.client.get("/api/status").headers["cache-control"], "no-store"
        )
        self.assertIn(
            "immutable",
            self.client.get("/static/app.js?v=perf3").headers["cache-control"],
        )

    def test_media_invalidation_and_detail_visibility(self):
        cache = web.FeedCache()
        before = cache.refresh()
        storage.save_media(
            self.pid,
            {
                "kind": "photo",
                "mime_type": "image/jpeg",
                "local_path": "photo.jpg",
                "telegram_file_id": "new",
            },
            self.db,
        )
        after = cache.refresh()
        self.assertNotEqual(after.revision, before.revision)
        self.assertNotEqual(after.etag, before.etag)
        detail = self.client.get("/api/events/" + self.eid).json()
        self.assertEqual(detail["feed"][0]["media"][0]["url"], "/media/photo.jpg")

    def test_detail_only_write_notifies_even_with_unchanged_feed_etag(self):
        cache = web.FeedCache()
        before = cache.refresh()
        with storage.connect(self.db) as conn:
            conn.execute(
                "UPDATE evidence_revisions SET text='Historical correction' WHERE post_id=?",
                (self.pid,),
            )
        after = cache.refresh()
        self.assertNotEqual(after.revision, before.revision)
        self.assertEqual(after.etag, before.etag)

    def test_expiry_invalidates_warmed_cache(self):
        before = self.client.get("/api/events")
        with storage.connect(self.db) as conn:
            conn.execute(
                "UPDATE events SET expires_at=? WHERE id=?",
                ((self._now() - timedelta(seconds=1)).isoformat(), self.eid),
            )
        after = self.client.get(
            "/api/events", headers={"If-None-Match": before.headers["etag"]}
        )
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.json(), [])
        self.assertEqual(self.client.get("/api/events/" + self.eid).status_code, 404)

    @staticmethod
    def _now():
        from datetime import UTC, datetime

        return datetime.now(UTC)


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def test_1000_subscribers_share_notifications_without_database_reads(self):
        hub = web.UpdateHub("initial")
        responses = []
        with (
            patch.object(web, "update_hub", hub),
            patch.object(
                web, "stream_revision", side_effect=AssertionError("per-subscriber SQL")
            ),
        ):
            for _ in range(1000):
                request = AsyncMock()
                request.is_disconnected.return_value = False
                responses.append(await web.stream(request))
            initial = await asyncio.gather(*(anext(r.body_iterator) for r in responses))
            self.assertTrue(all("data: initial" in item for item in initial))
            waiting = [asyncio.create_task(anext(r.body_iterator)) for r in responses]
            await asyncio.sleep(0)
            await hub.publish("changed")
            updates = await asyncio.wait_for(asyncio.gather(*waiting), 10)
            self.assertTrue(all("data: changed" in item for item in updates))
            await asyncio.gather(*(r.body_iterator.aclose() for r in responses))

    async def test_heartbeat_and_database_outage_disconnect(self):
        hub = web.UpdateHub("initial", heartbeat=0.01)
        request = AsyncMock()
        request.is_disconnected.return_value = False
        with patch.object(web, "update_hub", hub):
            response = await web.stream(request)
            await anext(response.body_iterator)
            self.assertIn("event: heartbeat", await anext(response.body_iterator))
            await hub.publish(None)
            with self.assertRaises(StopAsyncIteration):
                await anext(response.body_iterator)
            self.assertEqual((await web.stream(request)).status_code, 503)
            await hub.publish("recovered")
            self.assertTrue(hub.available)

    async def test_one_poller_recovers_from_database_error(self):
        hub = web.UpdateHub("initial", interval=0.01)
        snapshot = web.FeedSnapshot("changed", b"[]", b"", "etag", "gzip-etag")
        with patch.object(
            web.feed_cache,
            "refresh",
            side_effect=[RuntimeError("locked"), snapshot, snapshot],
        ) as refresh:
            task = asyncio.create_task(hub.poll())
            try:
                with self.assertLogs("web", level="ERROR"):
                    for _ in range(100):
                        if not hub.available:
                            break
                        await asyncio.sleep(0.002)
                self.assertFalse(hub.available)
                for _ in range(100):
                    if hub.revision == "changed":
                        break
                    await asyncio.sleep(0.002)
                self.assertTrue(hub.available)
                self.assertEqual(hub.revision, "changed")
                self.assertGreaterEqual(refresh.call_count, 2)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "test.sqlite3"
        storage.init_db(self.db)
        self.source = storage.ensure_source(
            {
                "key": "test",
                "name": "Тест",
                "username": "test",
                "timezone": "Europe/Moscow",
            },
            self.db,
        )
        self.time = datetime.now(UTC)

    def post(self, message=1, text="Пожар на улице Ленина", **extra):
        return {
            "source_id": self.source,
            "chat_id": 1,
            "message_id": message,
            "posted_at": self.time.isoformat(),
            "text": text,
            "raw_json": {},
            "link": "https://t.me/test/" + str(message),
            **extra,
        }

    def decision(self, **extra):
        return {
            "action": "create",
            "kind": "fire",
            "severity": "danger",
            "title": "Пожар",
            "description": "Источник сообщает о пожаре",
            "location_label": "Ленина",
            "location_precision": "exact",
            "latitude": 58.0,
            "longitude": 56.0,
            "display_minutes": 1440,
            **extra,
        }

    def event(self):
        pid, _ = storage.save_post(self.post(), self.db)
        eid = storage.apply_decision(pid, self.decision(), self.db, expected_revision=1)
        return pid, eid

    def test_duplicate_raw_edit_and_older_edit(self):
        pid, changed = storage.save_post(self.post(), self.db)
        self.assertTrue(changed)
        self.assertEqual(storage.save_post(self.post(), self.db), (pid, False))
        edited = (self.time + timedelta(minutes=1)).isoformat()
        self.assertTrue(
            storage.save_post(
                self.post(raw_json={"has_media": True}, edited_at=edited), self.db
            )[1]
        )
        self.assertEqual(storage.get_post(pid, self.db)["revision"], 2)
        self.assertFalse(storage.save_post(self.post(text="old"), self.db)[1])
        self.assertIsNone(
            storage.apply_decision(pid, self.decision(), self.db, expected_revision=1)
        )
        self.assertIn(pid, storage.pending_post_ids(self.db))

    def test_ignore_preserves_accepted_snapshot(self):
        pid, eid = self.event()
        storage.save_post(
            self.post(
                text="Отмена сообщения",
                edited_at=(self.time + timedelta(minutes=1)).isoformat(),
            ),
            self.db,
        )
        storage.apply_decision(pid, {"action": "ignore"}, self.db, expected_revision=2)
        event = storage.get_event(eid, self.db)
        self.assertEqual(event["feed"][0]["text"], "Пожар на улице Ленина")
        self.assertEqual(event["feed"][0]["revision"], 1)
        self.assertEqual(storage.event_for_post(pid, self.db), eid)
        other, _ = storage.save_post(self.post(message=2, text="Реклама"), self.db)
        storage.apply_decision(other, {"action": "ignore"}, self.db)
        self.assertEqual(storage.get_post(other, self.db)["text"], "")
        self.assertFalse(
            storage.save_post(self.post(message=2, text="Реклама"), self.db)[1]
        )

    def test_edit_association_null_location_and_content_without_ttl(self):
        pid, eid = self.event()
        before = storage.get_event(eid, self.db)
        storage.save_post(
            self.post(
                text="Уточнение",
                edited_at=(self.time + timedelta(minutes=2)).isoformat(),
            ),
            self.db,
        )
        result = storage.apply_decision(
            pid,
            self.decision(
                title="Уточненный пожар",
                latitude=None,
                longitude=None,
                location_label=None,
                location_precision="unknown",
                extends_lifetime=False,
            ),
            self.db,
            expected_revision=2,
        )
        after = storage.get_event(eid, self.db)
        self.assertEqual(result, eid)
        self.assertEqual(after["title"], "Уточненный пожар")
        self.assertEqual(after["expires_at"], before["expires_at"])
        self.assertEqual(after["latitude"], 58)
        self.assertEqual(after["location_precision"], "exact")
        older, _ = storage.save_post(
            self.post(
                message=2, posted_at=(self.time - timedelta(hours=2)).isoformat()
            ),
            self.db,
        )
        storage.apply_decision(
            older,
            self.decision(action="append", event_id=eid, extends_lifetime=True),
            self.db,
        )
        self.assertEqual(
            storage.get_event(eid, self.db)["expires_at"], before["expires_at"]
        )

    def test_unresolved_place_correction_clears_previous_point(self):
        _, eid = self.event()
        changed, _ = storage.save_post(self.post(message=2), self.db)
        storage.apply_decision(
            changed,
            self.decision(
                action="append",
                event_id=eid,
                latitude=None,
                longitude=None,
                location_label="  ЛЕНИНА ",
                location_precision="unknown",
                extends_lifetime=False,
            ),
            self.db,
        )
        self.assertEqual(storage.get_event(eid, self.db)["latitude"], 58)
        corrected, _ = storage.save_post(self.post(message=3), self.db)
        storage.apply_decision(
            corrected,
            self.decision(
                action="append",
                event_id=eid,
                latitude=None,
                longitude=None,
                location_label="Неизвестный переулок",
                location_query="Пермь неизвестный переулок",
                location_precision="unknown",
                location_source="failed",
                extends_lifetime=False,
            ),
            self.db,
        )
        event = storage.get_event(eid, self.db)
        self.assertEqual(event["location_label"], "Неизвестный переулок")
        self.assertEqual(event["location_precision"], "unknown")
        for field in (
            "latitude",
            "longitude",
            "radius_m",
            "location_provider",
            "location_confidence",
        ):
            self.assertIsNone(event[field])
        # Canonical query equality preserves an accepted point even when the display wording differs.
        other, _ = storage.save_post(self.post(message=4), self.db)
        other_event = storage.apply_decision(
            other, self.decision(location_query="Пермь Ленина"), self.db
        )
        followup, _ = storage.save_post(self.post(message=5), self.db)
        storage.apply_decision(
            followup,
            self.decision(
                action="append",
                event_id=other_event,
                location_label="улица Ленина",
                location_query="  пермь ЛЕНИНА ",
                latitude=None,
                longitude=None,
                location_precision="unknown",
            ),
            self.db,
        )
        self.assertEqual(storage.get_event(other_event, self.db)["latitude"], 58)

    def test_media_comments_revisions_and_queue(self):
        pid, eid = self.event()
        revision = storage.stream_revision(self.db)
        storage.save_media(
            pid,
            {
                "kind": "photo",
                "telegram_file_id": "1",
                "mime_type": "image/jpeg",
                "local_path": "a.jpg",
            },
            self.db,
        )
        self.assertNotEqual(storage.stream_revision(self.db), revision)
        revision = storage.stream_revision(self.db)
        comment, _ = storage.save_post(
            self.post(message=2, text="Комментарий", parent_post_id=pid), self.db
        )
        self.assertNotIn(comment, storage.pending_post_ids(self.db))
        storage.attach_comment(pid, comment, self.db)
        self.assertNotEqual(storage.stream_revision(self.db), revision)
        event = storage.get_event(eid, self.db)
        self.assertEqual(event["message_count"], 2)
        self.assertEqual(event["source_count"], 1)
        storage.set_media_path(pid, "1", "b.jpg", self.db)
        self.assertEqual(
            storage.get_event(eid, self.db)["feed"][0]["media"][0]["url"],
            "/media/b.jpg",
        )

    def test_comment_edit_and_attach_repair_are_live_and_idempotent(self):
        pid, eid = self.event()
        comment, _ = storage.save_post(
            self.post(message=2, text="Первый комментарий", parent_post_id=pid), self.db
        )
        storage.attach_comment(pid, comment, self.db)
        revision = storage.stream_revision(self.db)
        storage.save_post(
            self.post(
                message=2,
                text="Исправленный комментарий",
                parent_post_id=pid,
                edited_at=(self.time + timedelta(minutes=1)).isoformat(),
            ),
            self.db,
        )
        self.assertNotEqual(storage.stream_revision(self.db), revision)
        self.assertEqual(
            storage.get_event(eid, self.db)["feed"][1]["text"],
            "Исправленный комментарий",
        )
        with storage.connect(self.db) as conn:
            conn.execute(
                "UPDATE event_posts SET accepted_text='stale',accepted_revision=1 WHERE post_id=?",
                (comment,),
            )
        storage.attach_comment(pid, comment, self.db)
        self.assertEqual(storage.get_event(eid, self.db)["feed"][1]["revision"], 2)
        revision = storage.stream_revision(self.db)
        storage.attach_comment(pid, comment, self.db)
        self.assertEqual(storage.stream_revision(self.db), revision)
        event = storage.get_event(eid, self.db)
        self.assertEqual(event["source_names"], ["Тест"])
        self.assertEqual(event["updates"], 2)
        self.assertEqual(storage.list_events(self.db)[0]["source_names"], ["Тест"])

    def test_blocked_retry_recovers_and_honors_provider_delay(self):
        pid, _ = storage.save_post(self.post(), self.db)
        error = RuntimeError("secret")
        error.retry_after = 120
        storage.schedule_retry(pid, error, blocked=True, path=self.db)
        retry = datetime.fromisoformat(
            storage.get_post(pid, self.db)["next_attempt_at"]
        )
        self.assertGreaterEqual((retry - self.time).total_seconds(), 120)
        self.assertNotIn(pid, storage.pending_post_ids(self.db))
        with storage.connect(self.db) as conn:
            conn.execute(
                "UPDATE posts SET next_attempt_at=? WHERE id=?",
                ((self.time - timedelta(minutes=1)).isoformat(), pid),
            )
        self.assertIn(pid, storage.pending_post_ids(self.db))
        error.retry_after = float("nan")
        storage.schedule_retry(pid, error, path=self.db)
        _old, _ = storage.save_post(
            self.post(
                message=3, posted_at=(self.time - timedelta(days=30)).isoformat()
            ),
            self.db,
        )
        summary = storage.status_summary(self.db)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["stale_pending"], 1)

    def test_reads_do_not_expire_or_delete(self):
        pid, eid = self.event()
        with storage.connect(self.db) as conn:
            conn.execute(
                "UPDATE events SET expires_at=? WHERE id=?",
                ((self.time - timedelta(hours=1)).isoformat(), eid),
            )
        storage.list_events(self.db)
        self.assertIsNone(storage.get_event(eid, self.db))
        with storage.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM events").fetchone()[0], "active"
            )
        self.assertEqual(storage.purge_expired_events(self.db), (1, 0))
        self.assertIsNotNone(storage.get_post(pid, self.db))

    def test_old_unassociated_queue_and_expired_internal_evidence(self):
        old, _ = storage.save_post(
            self.post(
                message=3, posted_at=(self.time - timedelta(days=30)).isoformat()
            ),
            self.db,
        )
        self.assertNotIn(old, storage.pending_post_ids(self.db))
        storage.save_post(
            self.post(
                message=3,
                posted_at=(self.time - timedelta(days=30)).isoformat(),
                edited_at=self.time.isoformat(),
                text="Новое сообщение",
            ),
            self.db,
        )
        self.assertIn(old, storage.pending_post_ids(self.db))
        _, eid = self.event()
        with storage.connect(self.db) as conn:
            conn.execute("UPDATE events SET status='expired' WHERE id=?", (eid,))
        self.assertIsNone(storage.get_event(eid, self.db))
        self.assertEqual(
            storage.get_event(eid, self.db, include_expired=True)["id"], eid
        )

    def test_retry_and_status_sanitize_errors(self):
        pid, _ = storage.save_post(self.post(), self.db)
        storage.schedule_retry(pid, ValueError("secret token"), path=self.db)
        self.assertNotIn(pid, storage.pending_post_ids(self.db))
        self.assertEqual(storage.get_post(pid, self.db)["last_error"], "ValueError")
        storage.worker_status("heartbeat", storage.now(), self.db)
        storage.worker_status("last_error", "secret token", self.db)
        self.assertNotIn("secret", json.dumps(storage.status_summary(self.db)))
        storage.schedule_retry(pid, RuntimeError(), blocked=True, path=self.db)
        self.assertEqual(storage.status_summary(self.db)["blocked"], 1)

    def test_migration_preserves_ids_and_fts_active_only(self):
        pid, eid = self.event()
        with storage.connect(self.db) as conn:
            # Recreate a pre-revision-counter schema, without modern triggers.
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                conn.execute(f'DROP TRIGGER "{row[0]}"')
            conn.execute("DROP TABLE public_revision")
            for table, columns in {
                "posts": [
                    "revision",
                    "content_hash",
                    "attempts",
                    "next_attempt_at",
                    "last_error",
                ],
                "events": [
                    "revision",
                    "location_query",
                    "location_provider",
                    "location_confidence",
                ],
                "event_posts": [
                    "accepted_text",
                    "accepted_raw_json",
                    "accepted_revision",
                ],
            }.items():
                for column in columns:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            for table in ("evidence_revisions", "event_search", "worker_state"):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("PRAGMA user_version=0")
        storage.init_db(self.db)
        self.assertEqual(storage.event_for_post(pid, self.db), eid)
        self.assertEqual(
            storage.get_event(eid, self.db)["feed"][0]["text"], "Пожар на улице Ленина"
        )
        self.assertEqual(storage.find_candidates("пожар", path=self.db)[0]["id"], eid)
        with storage.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            conn.execute("UPDATE events SET status='expired' WHERE id=?", (eid,))
        self.assertEqual(storage.find_candidates("пожар", path=self.db), [])

    def test_unsafe_links_and_media_not_exposed(self):
        pid, _ = storage.save_post(self.post(link="javascript:alert(1)"), self.db)
        eid = storage.apply_decision(pid, self.decision(), self.db)
        storage.save_media(
            pid,
            {"kind": "photo", "local_path": "../.env", "telegram_file_id": "1"},
            self.db,
        )
        feed = storage.get_event(eid, self.db)["feed"][0]
        self.assertIsNone(feed["link"])
        self.assertEqual(feed["media"], [])

    def test_legacy_city_pin_hidden_and_reprocessing_keeps_evidence_and_ttl(self):
        pid, eid = self.event()
        with storage.connect(self.db) as conn:
            conn.execute(
                "UPDATE events SET location_precision='city',location_label='Пермь' WHERE id=?",
                (eid,),
            )
        before = storage.get_event(eid, self.db)
        self.assertIsNone(before["latitude"])
        self.assertIsNone(storage.list_events(self.db)[0]["longitude"])
        storage.apply_decision(
            pid,
            self.decision(
                action="append",
                event_id=eid,
                location_query="Пермь",
                location_label="Пермь",
                location_precision="city",
                latitude=None,
                longitude=None,
                reset_location=True,
                extends_lifetime=False,
            ),
            self.db,
        )
        after = storage.get_event(eid, self.db)
        self.assertEqual(after["location_label"], "Пермь")
        self.assertIsNone(after["latitude"])
        self.assertEqual(after["expires_at"], before["expires_at"])
        self.assertEqual(after["feed"], before["feed"])


if __name__ == "__main__":
    unittest.main()

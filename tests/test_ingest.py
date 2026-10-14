import asyncio
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ingest
import storage


class IngestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "db.sqlite3"
        storage.init_db(self.db)
        self.source = {
            "key": "test",
            "name": "Тест",
            "username": "testsource",
            "timezone": "Europe/Moscow",
        }
        self.message = SimpleNamespace(
            id=1,
            chat_id=1,
            date=datetime.now(UTC),
            edit_date=None,
            raw_text="Пожар",
            media=object(),
            photo=True,
            file=SimpleNamespace(id=123, mime_type="image/jpeg"),
            grouped_id=None,
        )

    def test_source_normalization_and_duplicate_validation(self):
        path = self.root / "sources.json"
        path.write_text(json.dumps([{**self.source, "username": "@testsource"}]))
        self.assertEqual(ingest.load_sources(path)[0]["username"], "testsource")
        for sources in [
            [],
            {},
            [self.source, {**self.source, "key": "other", "username": "TESTSOURCE"}],
            [{**self.source, "username": "https://t.me/testsource"}],
            [{**self.source, "timezone": "Europe/Imaginary"}],
        ]:
            path.write_text(json.dumps(sources))
            with (
                self.subTest(sources=sources),
                self.assertRaises((ValueError, KeyError)),
            ):
                ingest.load_sources(path)

    def test_backfill_recovers_transport_failure_without_losing_sources(self):
        operation = AsyncMock(side_effect=[OSError("temporary transport"), None])

        async def check():
            await ingest.backfill_with_retry(
                "client",
                [self.source],
                {"test": "entity"},
                "enqueue",
                36,
                asyncio.Event(),
            )

        with (
            patch.object(ingest, "sync_today", new=operation),
            patch.object(ingest.asyncio, "sleep", new=AsyncMock()) as sleep,
            patch.object(ingest, "worker_status"),
        ):
            asyncio.run(check())
        self.assertEqual(operation.await_count, 2)
        self.assertEqual(operation.call_args.args[1], [self.source])
        sleep.assert_awaited_once_with(10)

    def test_public_history_polling_tracks_new_messages_and_accepted_edits(self):
        async def newer(*args, **kwargs):
            self.assertEqual(kwargs["min_id"], 20)
            yield SimpleNamespace(id=21)

        client = SimpleNamespace(
            get_messages=AsyncMock(
                side_effect=[[SimpleNamespace(id=20)], [SimpleNamespace(id=15)]]
            ),
            iter_messages=newer,
        )
        cursors = {}
        enqueue = unittest.mock.Mock()
        with (
            patch.object(
                ingest,
                "event_source_posts_for_comments",
                side_effect=[[], [{"username": "testsource", "message_id": 15}]],
            ),
            patch.object(ingest, "is_source_message", return_value=True),
            patch.object(
                ingest, "store_message", side_effect=lambda m, s: (m.id, True)
            ),
            patch.object(ingest, "worker_status"),
        ):

            async def check():
                await ingest.poll_sources(
                    client, [self.source], {"test": "entity"}, enqueue, cursors
                )
                await ingest.poll_sources(
                    client, [self.source], {"test": "entity"}, enqueue, cursors
                )

            asyncio.run(check())
        self.assertEqual(cursors, {"test": 21})
        self.assertEqual([c.args[0] for c in enqueue.call_args_list], [20, 21, 15])

    def test_live_first_then_newest_backfill(self):
        early = datetime.now(UTC)
        late = early + timedelta(hours=1)
        queued = [
            ("backfill-late", ingest.queue_priority(late.isoformat(), live=False)),
            ("live-early", ingest.queue_priority(early.isoformat(), live=True)),
            ("backfill-early", ingest.queue_priority(early.isoformat(), live=False)),
            ("live-late", ingest.queue_priority(late.isoformat(), live=True)),
        ]
        self.assertEqual(
            [name for name, _ in sorted(queued, key=lambda row: row[1])],
            ["live-late", "live-early", "backfill-late", "backfill-early"],
        )

    def test_media_download_requires_accepted_active_event(self):
        source = storage.ensure_source(self.source, self.db)
        pid, _ = storage.save_post(
            {
                "source_id": source,
                "chat_id": 1,
                "message_id": 1,
                "posted_at": self.message.date.isoformat(),
                "text": "Пожар",
                "raw_json": {},
            },
            self.db,
        )
        storage.save_media(pid, ingest.media_items(self.message)[0], self.db)
        client = SimpleNamespace(
            download_media=AsyncMock(return_value=str(self.root / "123.jpg"))
        )
        with (
            patch.object(
                ingest, "connect", side_effect=lambda: storage.connect(self.db)
            ),
            patch.object(ingest, "settings", SimpleNamespace(media=self.root)),
            patch.object(ingest, "set_media_path") as set_path,
        ):
            asyncio.run(ingest.download_media(client, self.message, pid))
            client.download_media.assert_not_called()
            eid = storage.apply_decision(
                pid,
                {
                    "action": "create",
                    "title": "Пожар",
                    "description": "Источник сообщает о пожаре",
                    "severity": "danger",
                    "kind": "fire",
                    "display_minutes": 1440,
                },
                self.db,
            )
            asyncio.run(ingest.download_media(client, self.message, pid))
            client.download_media.assert_awaited_once()
            set_path.assert_called_once_with(pid, "123", "123.jpg")
            with storage.connect(self.db) as conn:
                conn.execute("UPDATE events SET status='expired' WHERE id=?", (eid,))
            asyncio.run(ingest.download_media(client, self.message, pid))
            self.assertEqual(client.download_media.await_count, 1)

    def test_comment_links_use_source_post_and_discussion_message_id(self):
        async def messages(*args, **kwargs):
            yield SimpleNamespace(id=77)

        client = SimpleNamespace(iter_messages=messages)
        parent = {"username": "testsource", "message_id": 42, "id": 9}
        with (
            patch.object(
                ingest, "event_source_posts_for_comments", return_value=[parent]
            ),
            patch.object(ingest, "store_message", return_value=(11, True)) as store,
            patch.object(ingest, "download_media", new=AsyncMock()) as download,
        ):
            asyncio.run(ingest.sync_comments(client, [self.source], {"test": "entity"}))
        self.assertEqual(
            store.call_args.kwargs["link"], "https://t.me/testsource/42?comment=77"
        )
        self.assertEqual(store.call_args.kwargs["parent_post_id"], 9)
        self.assertTrue(store.call_args.kwargs["comment"])
        download.assert_awaited_once()

    def test_comment_storage_cannot_replace_link_with_channel_comment_id(self):
        with (
            patch.object(ingest, "ensure_source", return_value=1),
            patch.object(ingest, "save_post", return_value=(2, True)) as save,
            patch.object(ingest, "save_media"),
            patch.object(ingest, "attach_comment") as attach,
        ):
            ingest.store_message(
                self.message,
                self.source,
                parent_post_id=9,
                comment=True,
                link="https://t.me/testsource/42?comment=1",
            )
        self.assertEqual(
            save.call_args.args[0]["link"], "https://t.me/testsource/42?comment=1"
        )
        attach.assert_called_once_with(9, 2)

    def test_credential_names_and_missing_error_do_not_expose_secret(self):
        with patch.dict(
            os.environ, {"TG_API_ID": "1234", "TG_API_HASH": "private-hash"}, clear=True
        ):
            self.assertEqual(ingest.require_env("TG_API_ID", int), 1234)
            self.assertEqual(ingest.require_env("TG_API_HASH"), "private-hash")
            with self.assertRaisesRegex(RuntimeError, "Missing TG_MISSING") as raised:
                ingest.require_env("TG_MISSING")
            self.assertNotIn("private-hash", str(raised.exception))
        self.assertEqual(
            ingest.message_link("testsource", 4), "https://t.me/testsource/4"
        )
        self.assertIsNone(ingest.message_link(None, 4))

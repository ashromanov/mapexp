"""Repair must not republish evidence, reset expiry, or overwrite concurrent updates."""

import json
import unittest
from unittest.mock import patch

import test_storage as fixtures
from test_mapping_recall import answers, candidate

import llm
import remap
import storage


class RemapTests(unittest.TestCase):
    post = fixtures.StorageTests.post
    decision = fixtures.StorageTests.decision

    def setUp(self):
        fixtures.StorageTests.setUp(self)
        self.text = "Сообщают о пожаре на шоссе Космонавтов, 113А"
        self.pid, _ = storage.save_post(self.post(text=self.text), self.db)
        decision = self.decision(
            latitude=None,
            longitude=None,
            location_precision="unknown",
            location_query="шоссе Космонавтов, 113А",
            location_kind="address",
            location_label="шоссе Космонавтов, 113А",
            audit={
                "location_evidence": "шоссе Космонавтов, 113А",
                "location_candidates": [candidate()],
                "calls": [{"answers": answers()}],
            },
        )
        self.eid = storage.apply_decision(self.pid, decision, self.db)

    def row(self):
        with storage.connect(self.db) as conn:
            return dict(
                conn.execute("SELECT * FROM events WHERE id=?", (self.eid,)).fetchone()
            )

    def test_remap_changes_only_geography_and_records_reason(self):
        before = self.row()
        evidence = storage.get_event(self.eid, self.db)["feed"]
        with (
            patch.object(remap.geocode, "geocode") as geocoder,
            patch.object(llm, "jev") as model,
        ):
            result = remap.remap_event(self.eid, self.db, apply=True)
            geocoder.assert_not_called()
            model.assert_not_called()
        self.assertEqual(result["status"], "mapped")
        after = self.row()
        for field in [
            "title",
            "description",
            "kind",
            "severity",
            "status",
            "first_reported_at",
            "last_confirmed_at",
            "expires_at",
        ]:
            self.assertEqual(after[field], before[field])
        detail = storage.get_event(self.eid, self.db)
        self.assertEqual(detail["feed"], evidence)
        self.assertEqual(detail["location_method"], "address_match")
        self.assertEqual(detail["latitude"], candidate()["latitude"])
        revision = storage.stream_revision(self.db)
        self.assertEqual(
            remap.remap_event(self.eid, self.db, apply=True)["status"], "unchanged"
        )
        self.assertEqual(storage.stream_revision(self.db), revision)

    def test_preview_does_not_change_event_or_revision(self):
        before = self.row()
        revision = storage.stream_revision(self.db)
        self.assertEqual(remap.remap_event(self.eid, self.db)["status"], "ready")
        self.assertEqual(self.row(), before)
        self.assertEqual(storage.stream_revision(self.db), revision)

    def test_concurrent_event_revision_is_not_overwritten(self):
        original = llm.select_location

        def concurrent(*args, **kwargs):
            result = original(*args, **kwargs)
            with storage.connect(self.db) as conn:
                conn.execute(
                    "UPDATE events SET description='New evidence',revision=revision+1 WHERE id=?",
                    (self.eid,),
                )
            return result

        with patch.object(llm, "select_location", side_effect=concurrent):
            self.assertEqual(
                remap.remap_event(self.eid, self.db, apply=True)["status"],
                "changed_during_remap",
            )
        self.assertIsNone(self.row()["latitude"])
        self.assertEqual(self.row()["description"], "New evidence")

    def test_missing_place_can_be_reextracted_without_replacing_summary(self):
        with storage.connect(self.db) as conn:
            row = conn.execute(
                "SELECT id,result_json FROM decisions WHERE event_id=?", (self.eid,)
            ).fetchone()
            payload = json.loads(row["result_json"])
            payload.update(location_query=None, location_kind="unknown")
            payload["audit"] = {}
            conn.execute(
                "UPDATE decisions SET result_json=? WHERE id=?",
                (json.dumps(payload), row["id"]),
            )
            conn.execute(
                "UPDATE events SET location_query=NULL WHERE id=?", (self.eid,)
            )
        before = self.row()
        generated = llm.EventDraft(
            usable=True,
            kind="fire",
            severity="danger",
            title="Different title",
            description="Different description",
            location_query="шоссе Космонавтов, 113А",
            location_kind="address",
            location_evidence="шоссе Космонавтов, 113А",
            display_minutes=1440,
        )
        with (
            patch.object(
                llm, "draft", return_value=(generated, {"stage": "location_reextract"})
            ),
            patch.object(remap.geocode, "geocode", return_value=[candidate()]),
            patch.object(llm, "jev", return_value=answers()),
        ):
            self.assertEqual(
                remap.remap_event(self.eid, self.db, apply=True, extract_missing=True)[
                    "status"
                ],
                "mapped",
            )
        self.assertEqual(self.row()["title"], before["title"])
        self.assertEqual(self.row()["description"], before["description"])
        self.assertEqual(self.row()["location_query"], "шоссе Космонавтов, 113А")

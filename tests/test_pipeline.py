import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import httpx
import llm
import pipeline
import storage


def selection(value="fire", probability=0.98, confidence=0.98):
    return {
        "type": "choice",
        "choice": value,
        "confidence": confidence,
        "probabilities": {value: probability, "other": 1 - probability},
    }


def triage(relevance=0.99):
    return {
        "local_incident": {"type": "noul", "noul": relevance},
        "kind": {"type": "choice", "choice": "fire"},
        "severity": {"type": "choice", "choice": "danger"},
    }


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.post = {
            "id": 1,
            "revision": 1,
            "parent_post_id": None,
            "source_name": "Тест",
            "posted_at": datetime.now(UTC).isoformat(),
            "text": "Пожар, адрес неизвестен",
        }
        self.draft = llm.EventDraft(
            usable=True,
            kind="fire",
            severity="danger",
            title="Пожар",
            description="Источник сообщает о пожаре",
            location_query=None,
            location_kind="unknown",
            location_evidence=None,
            display_minutes=1440,
        )
        for name, value in [
            ("get_post", self.post),
            ("event_for_post", None),
            ("find_candidates", []),
        ]:
            mock = patch.object(llm, name, return_value=value).start()
            self.addCleanup(patch.stopall)
            setattr(self, name, mock)

    def test_confident_rejection_skips_text_and_geocoder(self):
        with (
            patch.object(llm, "jev", return_value=triage(0.01)),
            patch.object(llm, "draft") as draft,
            patch.object(llm, "geocode") as geocode,
        ):
            result = llm.decide(1)
        self.assertEqual(result.action, "ignore")
        draft.assert_not_called()
        geocode.assert_not_called()
        self.assertIsNone(result.latitude)

    def test_media_only_is_not_given_coordinates(self):
        self.post["text"] = ""
        self.post["raw_json"] = {"has_media": True}
        with (
            patch.object(llm, "jev", return_value=triage(0)),
            patch.object(llm, "draft") as draft,
        ):
            result = llm.decide(1)
        self.assertEqual(result.action, "ignore")
        self.assertIsNone(result.longitude)
        draft.assert_not_called()

    def test_accepted_edit_keeps_string_association_even_if_triage_rejects(self):
        self.event_for_post.return_value = "incident-uuid"
        existing = {"id": "incident-uuid", "title": "Пожар", "feed": []}
        verification = {
            "local_scope": {"noul": 0.99},
            "supported": {"noul": 0.99},
            "new_facts": {"noul": 0.99},
        }
        with (
            patch.object(llm, "jev", side_effect=[triage(0.01), verification]),
            patch.object(llm, "get_event", return_value=existing) as get_event,
            patch.object(llm, "draft", return_value=(self.draft, {})),
        ):
            result = llm.decide(1)
        self.assertEqual(result.action, "append")
        self.assertEqual(result.event_id, "incident-uuid")
        get_event.assert_called_once_with("incident-uuid", include_expired=True)
        self.find_candidates.assert_not_called()

    def test_ambiguous_same_place_candidate_creates_separate_incident(self):
        self.find_candidates.return_value = [
            {"id": "old-id", "title": "Пожар", "location_label": "Ленина"}
        ]
        match = {
            "incident": {
                "choice": "old-id",
                "confidence": 0.99,
                "probabilities": {"old-id": 0.6, "new": 0.4},
            }
        }
        verification = {
            "local_scope": {"noul": 0.99},
            "supported": {"noul": 0.99},
            "new_facts": {"noul": 0.99},
        }
        with (
            patch.object(llm, "jev", side_effect=[triage(), match, verification]),
            patch.object(llm, "draft", return_value=(self.draft, {})),
        ):
            result = llm.decide(1)
        self.assertEqual(result.action, "create")
        self.assertIsNone(result.event_id)
        self.assertIsNone(result.latitude)

    def test_foreign_incident_is_rejected_despite_local_channel_branding(self):
        self.post["text"] = "В Омске пожар. ЧП Пермь @testsource"
        verification = {
            "local_scope": {"noul": 0.02},
            "supported": {"noul": 0.99},
            "new_facts": {"noul": 0.99},
        }
        with (
            patch.object(llm, "jev", side_effect=[triage(0.2), verification]),
            patch.object(llm, "draft", return_value=(self.draft, {})),
        ):
            result = llm.decide(1)
        self.assertEqual(result.action, "ignore")
        self.assertIsNone(result.latitude)

    def test_unsupported_summary_repairs_once_then_uses_exact_source(self):
        verification = {
            "local_scope": {"noul": 0.99},
            "supported": {"noul": 0.4},
            "new_facts": {"noul": 0.99},
        }
        with (
            patch.object(
                llm, "jev", side_effect=[triage(), verification, verification]
            ),
            patch.object(llm, "draft", return_value=(self.draft, {})) as draft,
        ):
            result = llm.decide(1)
        self.assertEqual(result.action, "create")
        self.assertIn(self.post["text"], result.description)
        self.assertTrue(result.audit["source_quote_fallback"])
        self.assertTrue(result.reset_location)
        self.assertEqual(draft.call_count, 2)
        self.assertTrue(draft.call_args.kwargs["repair"])

    def test_answer_schema_and_finite_probability_validation(self):
        questions = {
            "incident": llm.choice("Choose", {"fire": "Fire", "other": "Other"})
        }
        self.assertEqual(
            llm.validate_answers({"answers": {"incident": selection()}}, questions)[
                "incident"
            ]["choice"],
            "fire",
        )
        for bad in [float("nan"), float("inf"), -0.1, 1.1, True, None]:
            with self.subTest(bad=bad), self.assertRaises(llm.ProviderFailure):
                llm.validate_answers(
                    {"answers": {"incident": selection(confidence=bad)}}, questions
                )
        for answers in [
            None,
            {},
            {"incident": {"type": "noul", "noul": 0.5}},
            {"incident": selection("unknown")},
            {"incident": {**selection(), "probabilities": {"fire": 0.8, "other": 0.8}}},
        ]:
            with self.subTest(answers=answers), self.assertRaises(llm.ProviderFailure):
                llm.validate_answers({"answers": answers}, questions)

    def test_jev_records_valid_provider_response_and_defers_malformed(self):
        questions = {
            "incident": llm.choice("Choose", {"fire": "Fire", "other": "Other"})
        }
        valid = {
            "answers": {"incident": selection()},
            "model": "typesafe/jev-test",
            "usage": {"cost": 0.01},
        }
        malformed = {
            "answers": {
                "incident": {
                    **selection(),
                    "probabilities": {"fire": "0.9", "other": 0.1},
                }
            }
        }
        with (
            patch.object(
                llm, "settings", SimpleNamespace(api_key="test", decision_model="test")
            ),
            patch.object(llm.httpx, "Client") as client,
        ):
            sender = client.return_value.__enter__.return_value.post
            sender.return_value = httpx.Response(200, json=valid)
            audit = []
            self.assertEqual(
                llm.jev({}, questions, audit)["incident"]["choice"], "fire"
            )
            self.assertEqual(audit[0]["model"], "typesafe/jev-test")
            self.assertEqual(audit[0]["usage"]["cost"], 0.01)
            for payload in [malformed, {"answers": None}]:
                sender.return_value = httpx.Response(200, json=payload)
                with (
                    self.subTest(payload=payload),
                    self.assertRaises(llm.ProviderFailure),
                ):
                    llm.jev({}, questions, [])
            sender.return_value = httpx.Response(200, text="invalid json")
            with self.assertRaises(llm.ProviderFailure):
                llm.jev({}, questions, [])

    def test_provider_429_retries_and_402_blocks_without_exposing_key(self):
        for status, error in [(429, llm.ProviderFailure), (402, llm.LLMBlocked)]:
            response = httpx.Response(
                status,
                headers={"retry-after": "120"},
                request=httpx.Request("POST", "https://example.test"),
            )
            with (
                self.subTest(status=status),
                patch.object(
                    llm,
                    "settings",
                    SimpleNamespace(
                        api_key="private-test-token", decision_model="test"
                    ),
                ),
                patch.object(llm.httpx, "Client") as client,
            ):
                client.return_value.__enter__.return_value.post.return_value = response
                with self.assertRaises(error) as raised:
                    llm.jev({}, {}, [])
                self.assertEqual(raised.exception.retry_after, 120)
                self.assertNotIn("private-test-token", str(raised.exception))
                if status == 429:
                    self.assertNotIsInstance(raised.exception, llm.LLMBlocked)

    def test_geocoder_does_not_make_district_report_exact(self):
        decision = llm.EventDecision(action="create", location_kind="district")
        result = llm.apply_geocoded_location(
            decision,
            [
                {
                    "precision": "address",
                    "label": "Ленина, 1",
                    "latitude": 58,
                    "longitude": 56,
                    "source": "test",
                }
            ],
            0,
        )
        self.assertEqual(result.location_precision, "district")
        self.assertEqual(result.radius_m, 3500)

    def test_contextual_landmark_maps_below_old_exact_match_threshold(self):
        self.post["text"] = "Красные казармы, стоит разбитый каршеринг"
        generated = self.draft.model_copy(
            update={
                "location_query": "Красные казармы",
                "location_kind": "district",
                "location_evidence": "Красные казармы, стоит разбитый каршеринг",
            }
        )
        places = [
            {
                "label": "Красные казармы, Пермь",
                "latitude": 57.9934442,
                "longitude": 56.2645451,
                "precision": "district",
                "source": "test",
            }
        ]
        verified = {
            "supported": {"noul": 0.99},
            "local_scope": {"noul": 0.99},
            "new_facts": {"noul": 0.99},
            "location_grounded": {"noul": 0.96},
            "location": {
                "choice": "0",
                "confidence": 0.86,
                "probabilities": {"0": 0.88, "none": 0.12},
            },
        }
        with (
            patch.object(llm, "draft", return_value=(generated, {})),
            patch.object(llm, "geocode", return_value=places),
            patch.object(llm, "jev", side_effect=[triage(), verified]),
        ):
            result = llm.decide(1)
        self.assertEqual(result.location_precision, "district")
        self.assertEqual(result.latitude, 57.9934442)
        self.assertEqual(result.location_confidence, 0.86)
        self.assertGreater(result.radius_m, 3500)
        self.assertFalse(result.reset_location)

    def test_city_only_arrest_never_looks_up_city_or_foreign_street(self):
        self.post["text"] = (
            "В Перми задержали подозреваемых в нападении на улице 8 Марта в Екатеринбурге."
        )
        generated = self.draft.model_copy(
            update={
                "location_query": "Пермь",
                "location_kind": "city",
                "location_evidence": "В Перми задержали",
            }
        )
        verified = {
            "supported": {"noul": 0.99},
            "local_scope": {"noul": 0.99},
            "new_facts": {"noul": 0.99},
        }
        with (
            patch.object(llm, "draft", return_value=(generated, {})),
            patch.object(llm, "geocode") as geocoder,
            patch.object(llm, "jev", side_effect=[triage(), verified]),
        ):
            result = llm.decide(1)
        geocoder.assert_not_called()
        self.assertIsNone(result.latitude)
        self.assertEqual(result.location_precision, "city")
        self.assertTrue(result.reset_location)

    def test_location_quote_must_exist_in_source_not_generated_prose(self):
        for quote in [None, "Возгорание на улице Ленина"]:
            generated = self.draft.model_copy(
                update={
                    "location_query": "Ленина",
                    "location_kind": "street",
                    "location_evidence": quote,
                }
            )
            with patch.object(llm, "geocode") as geocoder:
                self.assertEqual(
                    llm.location_candidates(generated, self.post, None), []
                )
            geocoder.assert_not_called()

    def test_accepted_same_incident_evidence_can_locate_an_update(self):
        generated = self.draft.model_copy(
            update={
                "location_query": "Ленина",
                "location_kind": "street",
                "location_evidence": "пожар на улице Ленина",
            }
        )
        with patch.object(
            llm, "geocode", return_value=[{"precision": "street"}]
        ) as geocoder:
            result = llm.location_candidates(
                generated,
                self.post,
                {"feed": [{"text": "Пожар на улице Ленина. Адрес дома уточняют."}]},
            )
        self.assertEqual(len(result), 1)
        geocoder.assert_called_once_with("Ленина")

    def test_foreign_episode_place_fails_grounding_even_with_confident_match(self):
        place = {"latitude": 58.0, "longitude": 56.2, "precision": "street"}
        answers = {
            "location_grounded": {"noul": 0.1},
            "location": {
                "choice": "0",
                "confidence": 0.99,
                "probabilities": {"0": 0.99, "none": 0.01},
            },
        }
        self.assertEqual(llm.select_location(answers, [place]), (None, None))

    def test_geographically_conflicting_candidates_do_not_get_one_pin(self):
        answers = {
            "location_grounded": {"noul": 0.95},
            "location": {
                "choice": "0",
                "confidence": 0.8,
                "probabilities": {"0": 0.7, "1": 0.25, "none": 0.05},
            },
        }
        places = [
            {"latitude": 58.0, "longitude": 56.2, "precision": "poi"},
            {"latitude": 58.2, "longitude": 56.4, "precision": "poi"},
        ]
        self.assertEqual(llm.select_location(answers, places), (None, None))
        places[1].update(latitude=58.001, longitude=56.201)
        self.assertEqual(llm.select_location(answers, places), (0, 0.7))
        self.assertGreater(places[0]["radius_m"], 100)

    def test_uncertain_building_match_becomes_area_and_city_never_maps(self):
        place = {
            "latitude": 58.0,
            "longitude": 56.2,
            "precision": "address",
            "label": "Ленина 5",
            "source": "test",
        }
        result = llm.apply_geocoded_location(
            llm.EventDecision(action="create", location_kind="address"),
            [place],
            0,
            0.75,
        )
        self.assertEqual(result.location_precision, "street")
        self.assertGreater(result.radius_m, 1000)
        result = llm.apply_geocoded_location(
            llm.EventDecision(action="create", location_kind="city"), [place], 0, 0.99
        )
        self.assertIsNone(result.latitude)


class PipelineRevisionTests(unittest.TestCase):
    def test_edit_during_decision_leaves_new_revision_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "db.sqlite3"
            storage.init_db(db)
            source = storage.ensure_source(
                {
                    "key": "test",
                    "name": "Тест",
                    "username": "testsource",
                    "timezone": "Europe/Moscow",
                },
                db,
            )
            stamp = datetime.now(UTC)
            post = {
                "source_id": source,
                "chat_id": 1,
                "message_id": 1,
                "posted_at": stamp.isoformat(),
                "text": "Пожар",
                "raw_json": {},
            }
            pid, _ = storage.save_post(post, db)

            def decide(_):
                storage.save_post(
                    {
                        **post,
                        "text": "Уточнение",
                        "edited_at": (stamp + timedelta(seconds=1)).isoformat(),
                    },
                    db,
                )
                return llm.EventDecision(
                    action="create",
                    title="Пожар",
                    description="Сообщается о пожаре",
                    audit={"revision": 1},
                )

            with (
                patch.object(
                    pipeline,
                    "get_post",
                    side_effect=lambda pid: storage.get_post(pid, db),
                ),
                patch.object(pipeline, "decide", side_effect=decide),
                patch.object(
                    pipeline,
                    "apply_decision",
                    side_effect=lambda pid, decision, **kw: storage.apply_decision(
                        pid, decision, db, **kw
                    ),
                ),
            ):
                self.assertIsNone(pipeline.process_post(pid))
            self.assertEqual(storage.list_events(db), [])
            self.assertEqual(storage.get_post(pid, db)["revision"], 2)
            self.assertEqual(storage.get_post(pid, db)["llm_status"], "pending")

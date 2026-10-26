"""Recall-oriented mapping still keeps source evidence and actual geocoder geometry."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import geocode
import llm
import storage


def candidate(
    house="113А", street="шоссе Космонавтов", lat=57.9913371, lon=56.2014218, **extra
):
    return {
        "label": f"{house}, {street}, Пермь",
        "latitude": lat,
        "longitude": lon,
        "precision": "address",
        "source": "test",
        "radius_m": 64,
        "address": {"city": "Пермь", "street": street, "house_number": house},
        **extra,
    }


def answers(choice="0", probability=0.79, confidence=0.58, grounded=0.83):
    return {
        "location_grounded": {"noul": grounded},
        "location": {
            "choice": choice,
            "confidence": confidence,
            "probabilities": {"0": probability, "none": 1 - probability},
        },
    }


class MappingRecallTests(unittest.TestCase):
    def test_full_address_is_mapped_despite_old_confidence_veto(self):
        places = [candidate()]
        resolution = {}
        index, confidence = llm.select_location(
            answers(),
            places,
            "шоссе Космонавтов, 113А",
            "«Ходячие мертвецы» на шоссе космонавтов 113а",
            resolution,
        )
        self.assertEqual(index, 0)
        self.assertEqual(confidence, 0.58)
        self.assertEqual(resolution["method"], "address_match")
        result = llm.apply_geocoded_location(
            llm.EventDecision(action="create", location_kind="address"),
            places,
            index,
            confidence,
        )
        self.assertEqual(result.latitude, 57.9913371)
        self.assertEqual(result.location_precision, "poi")
        self.assertLess(result.radius_m, 500)

    def test_full_address_can_override_model_none_but_not_unrelated_context(self):
        for grounded, expected in [(0.8, 0), (0.1, None)]:
            selected, _ = llm.select_location(
                answers("none", 0.1, 0.2, grounded),
                [candidate()],
                "шоссе Космонавтов, 113А",
                "на шоссе Космонавтов 113А",
            )
            self.assertEqual(selected, expected)

    def test_house_suffixes_corpuses_city_and_street_are_not_interchangeable(self):
        query = "шоссе Космонавтов, 113А"
        for house in ["113", "113Б", "113/1", "113 корпус 2"]:
            self.assertFalse(geocode.address_matches(query, candidate(house)))
        self.assertFalse(
            geocode.address_matches(query, candidate(street="улица Космонавтов"))
        )
        place = candidate()
        place["address"]["city"] = "Москва"
        self.assertFalse(geocode.address_matches(query, place))
        self.assertTrue(geocode.address_matches(query, candidate("113A")))
        self.assertTrue(
            geocode.address_matches(
                "улица Ленина, 5 корпус 2", candidate("5к2", "улица Ленина")
            )
        )
        self.assertFalse(
            geocode.address_matches(
                "улица Ленина, 5 корпус 2", candidate("5к3", "улица Ленина")
            )
        )

    def test_cached_legacy_candidate_without_structured_address_still_matches(self):
        place = candidate()
        place.pop("address")
        self.assertTrue(geocode.address_matches("шоссе Космонавтов, 113А", place))

    def test_same_house_poi_and_building_are_one_address_cluster(self):
        places = [candidate(), candidate(lat=57.9914, lon=56.2015)]
        result = {}
        selected, _ = llm.select_location(
            answers(),
            places,
            "шоссе Космонавтов 113А",
            "шоссе Космонавтов 113А",
            result,
        )
        self.assertEqual(selected, 0)
        self.assertEqual(result["method"], "address_match")

    def test_distinct_matching_addresses_are_approximate_not_exact(self):
        places = [candidate(), candidate(lat=58.1, lon=56.3)]
        value = answers()
        value["location"]["probabilities"] = {"0": 0.45, "1": 0.4, "none": 0.15}
        selected, confidence = llm.select_location(
            value, places, "шоссе Космонавтов 113А", "шоссе Космонавтов 113А"
        )
        result = llm.apply_geocoded_location(
            llm.EventDecision(action="create", location_kind="address"),
            places,
            selected,
            confidence,
        )
        self.assertEqual(selected, 0)
        self.assertEqual(result.location_precision, "area")
        self.assertGreater(result.radius_m, 10000)

    def test_nearby_wording_never_becomes_an_exact_building(self):
        places = [candidate()]
        selected, confidence = llm.select_location(
            answers(confidence=0.99, probability=0.99, grounded=0.99),
            places,
            "шоссе Космонавтов 113А",
            "возле дома на шоссе Космонавтов 113А",
        )
        result = llm.apply_geocoded_location(
            llm.EventDecision(action="create", location_kind="address"),
            places,
            selected,
            confidence,
        )
        self.assertEqual(result.location_precision, "poi")

    def test_low_scoring_landmark_can_use_name_match(self):
        place = {
            "label": "ЦУМ, Пермь",
            "latitude": 58.011,
            "longitude": 56.238,
            "precision": "poi",
            "source": "test",
        }
        resolution = {}
        selected, confidence = llm.select_location(
            answers("none", 0.3, 0.25, 0.6),
            [place],
            "ЦУМ, Пермь",
            "около ЦУМа",
            resolution,
        )
        self.assertEqual(selected, 0)
        self.assertEqual(resolution["method"], "text_approximate")
        self.assertLess(confidence, 0.6)

    def test_intersection_and_multiple_districts_keep_partial_approximate_matches(self):
        for query, label, precision in [
            (
                "перекрёсток улиц Окулова и Дзержинского",
                "улица Окулова, Пермь",
                "street",
            ),
            (
                "Индустриальный и Ленинский районы Перми",
                "Индустриальный район, Пермь",
                "district",
            ),
        ]:
            place = {
                "label": label,
                "latitude": 58.01,
                "longitude": 56.25,
                "precision": precision,
                "source": "test",
            }
            index, confidence = llm.select_location(
                answers("none", 0.2, 0.3, 0.8), [place], query, query
            )
            self.assertEqual(index, 0)
            result = llm.apply_geocoded_location(
                llm.EventDecision(action="create", location_kind=precision),
                [place],
                index,
                confidence,
            )
            self.assertNotEqual(result.location_precision, "exact")
            self.assertGreaterEqual(result.radius_m, 2000)

    def test_missing_geometry_or_unrelated_name_does_not_invent_coordinates(self):
        self.assertEqual(
            llm.select_location(answers(), [], "Ленина 5", "Ленина 5"), (None, None)
        )
        place = {
            "label": "Рыбный магазин, Пермь",
            "latitude": 58.01,
            "longitude": 56.25,
            "precision": "poi",
        }
        self.assertEqual(
            llm.select_location(answers("none", 0.1), [place], "ЦУМ", "у ЦУМа"),
            (None, None),
        )

    def test_partial_first_provider_result_triggers_fallback_and_keeps_best(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.sqlite3"
            storage.init_db(path)
            weak = candidate("113")
            strong = candidate()
            with (
                patch.object(geocode, "_photon", return_value=[weak]) as photon,
                patch.object(geocode, "_nominatim", return_value=[strong]) as fallback,
                patch.object(geocode, "_open_meteo") as city,
            ):
                result = geocode.geocode("шоссе Космонавтов 113А", path)
                self.assertEqual(result[0]["address"]["house_number"], "113А")
                self.assertGreaterEqual(photon.call_count, 1)
                self.assertEqual(fallback.call_count, 1)
                city.assert_not_called()
                geocode.geocode("шоссе Космонавтов 113А", path)
                self.assertEqual(fallback.call_count, 1)

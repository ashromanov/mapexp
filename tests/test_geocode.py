import unittest
from unittest.mock import patch
import geocode
from llm import EventDecision, apply_geocoded_location


class GeocodeTests(unittest.TestCase):
    def test_colloquial_object_abbreviations_are_searchable_not_business_homonyms(self):
        self.assertEqual(
            geocode.normalize_location("ТРК «Планета»"),
            "торгово-развлекательный центр Планета, Пермь",
        )
        self.assertEqual(
            geocode.normalize_location('ТЦ "Лето"'), "торговый центр Лето, Пермь"
        )
        self.assertIn("жилой комплекс", geocode.normalize_location("ЖК Каменный"))

    def test_river_and_named_natural_area_use_area_not_unknown(self):
        for category, kind in [
            ("waterway", "river"),
            ("natural", "water"),
            ("landuse", "forest"),
        ]:
            self.assertEqual(geocode._precision(category, kind), "area")

    def test_street_without_section_covers_known_segments(self):
        candidates = [
            geocode._candidate(
                "Ленина, Пермь", 58.01, 56.25, "street", "test", radius_m=100
            ),
            geocode._candidate(
                "Ленина, Пермь", 58.01, 56.31, "street", "test", radius_m=250
            ),
        ]
        result = apply_geocoded_location(
            EventDecision(action="create", location_kind="street"), candidates, 0, 0.9
        )
        self.assertGreater(result.radius_m, 3500)
        self.assertLessEqual(result.radius_m, 50000)

    def test_unrepresentable_territory_is_not_truncated_into_false_circle(self):
        self.assertIsNone(
            geocode._candidate(
                "Большая территория",
                58.01,
                56.25,
                "area",
                "test",
                bounds=[55, 59, 57, 57],
            )
        )
        result = apply_geocoded_location(
            EventDecision(action="create", location_kind="area"),
            [
                {
                    "label": "Большая территория",
                    "latitude": 58.01,
                    "longitude": 56.25,
                    "precision": "area",
                    "radius_m": 49000,
                    "source": "test",
                }
            ],
            0,
            0.7,
        )
        self.assertIsNone(result.latitude)
        self.assertTrue(result.reset_location)

    def test_cemetery_duplicate_wood_keeps_known_landmark(self):
        features = []
        for category, kind in [("natural", "wood"), ("landuse", "cemetery")]:
            features.append(
                {
                    "geometry": {"coordinates": [56.2817198, 57.9918105]},
                    "properties": {
                        "name": "Южное кладбище",
                        "city": "Пермь",
                        "osm_key": category,
                        "osm_value": kind,
                        "extent": [56.27554, 57.9953643, 56.2912224, 57.9877454],
                    },
                }
            )
        with patch.object(
            geocode, "_request_json", return_value={"features": features}
        ):
            results = geocode._dedupe(geocode._photon("Южное кладбище"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["precision"], "poi")
        self.assertGreater(results[0]["radius_m"], 600)
        mapped = apply_geocoded_location(
            EventDecision(action="create", location_kind="poi"), results, 0
        )
        self.assertEqual(mapped.location_precision, "poi")
        self.assertEqual(mapped.radius_m, results[0]["radius_m"])

    def test_close_distinct_addresses_are_not_discarded(self):
        points = [
            geocode._candidate(label, 58.01, 56.25, "address", "test")
            for label in ["Ленина, 5", "Ленина, 7"]
        ]
        self.assertEqual(len(geocode._dedupe(points)), 2)

    def test_segments_of_same_named_river_do_not_compete_with_each_other(self):
        points = [
            geocode._candidate(
                "Данилиха, Пермь",
                lat,
                lon,
                "area",
                "test",
                feature_type="waterway/river",
                radius_m=radius,
            )
            for lat, lon, radius in [(58.001, 56.203, 191), (57.976, 56.223, 5045)]
        ]
        points.append(
            geocode._candidate("Данилиха, Пермь", 58.0, 56.2, "district", "test")
        )
        results = geocode._dedupe(points)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["precision"], "area")
        self.assertGreater(results[0]["radius_m"], 7500)
        self.assertEqual(results[1]["precision"], "district")

    def test_invalid_bounds_do_not_invent_radius_or_crash(self):
        for bounds in [
            [0, 0, "bad", 0],
            [float("nan"), 0, 56, 58],
            [56, 91, 57, 57],
            None,
        ]:
            candidate = geocode._candidate(
                "test", 58.01, 56.25, "poi", "test", bounds=bounds
            )
            self.assertNotIn("radius_m", candidate)
        self.assertIsNone(geocode._candidate("bad", float("inf"), 56, "poi", "test"))

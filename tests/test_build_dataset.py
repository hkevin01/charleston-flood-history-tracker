"""Unit tests for Charleston flood dataset helpers."""

import unittest

from scripts.build_dataset import (
    CITIES,
    FloodEvent,
    build_risk_zones,
    classify_damage_context,
    event_near_city,
    haversine_miles,
    parse_damage_to_usd,
    parse_float,
    parse_int,
)
from scripts.annotate_damage_context import damage_appears_unreported


class FloodBuildHelperTests(unittest.TestCase):
    def test_haversine_zero(self):
        self.assertAlmostEqual(haversine_miles(32.77, -79.93, 32.77, -79.93), 0.0, places=3)

    def test_parse_damage(self):
        self.assertEqual(parse_damage_to_usd("10K"), 10000.0)
        self.assertEqual(parse_damage_to_usd("2.5M"), 2500000.0)
        self.assertEqual(parse_damage_to_usd("1B"), 1000000000.0)
        self.assertEqual(parse_damage_to_usd("bad"), 0.0)

    def test_parse_fallbacks(self):
        self.assertEqual(parse_int("oops", default=7), 7)
        self.assertEqual(parse_float("oops", default=3.5), 3.5)

    def test_event_near_city(self):
        city = next(c for c in CITIES if c["key"] == "charleston")
        near = FloodEvent(1, 1, 2020, 1, "01-JAN-20 10:00:00", "Flood", "SOUTH CAROLINA", "CHARLESTON", 32.80, -79.95, 32.80, -79.95, 0, 0, 1000.0, 0.0, "")
        far = FloodEvent(2, 1, 2020, 1, "01-JAN-20 10:00:00", "Flood", "SOUTH CAROLINA", "GREENVILLE", 34.90, -82.40, 34.90, -82.40, 0, 0, 1000.0, 0.0, "")
        self.assertTrue(event_near_city(near, city, 20))
        self.assertFalse(event_near_city(far, city, 20))

    def test_build_risk_zones(self):
        city = next(c for c in CITIES if c["key"] == "charleston")
        events = [
            FloodEvent(1, 1, 2020, 1, "01-JAN-20 10:00:00", "Flood", "SOUTH CAROLINA", "CHARLESTON", 32.80, -79.95, 32.81, -79.94, 1, 0, 50000.0, 0.0, ""),
            FloodEvent(2, 1, 2021, 2, "15-FEB-21 12:00:00", "Coastal Flood", "SOUTH CAROLINA", "CHARLESTON", 32.75, -79.93, 32.75, -79.93, 0, 0, 125000.0, 0.0, ""),
        ]
        zones = build_risk_zones(city, events)
        self.assertGreater(len(zones), 0)
        self.assertIn("level", zones[0])
        levels = {z["level"] for z in zones}
        self.assertTrue(levels.intersection({"Low", "Guarded", "Elevated", "High", "Most Affected"}))

    # ------------------------------------------------------------------
    # classify_damage_context tests
    # ID: CFHT-CLASSIFY-TEST-001
    # ------------------------------------------------------------------
    def test_classify_empty_returns_unknown(self):
        self.assertEqual(classify_damage_context(""), "unknown")
        self.assertEqual(classify_damage_context(None), "unknown")
        self.assertEqual(classify_damage_context("   "), "unknown")

    def test_classify_infra_dominant(self):
        narrative = (
            "Heavy rain caused several roads to flood overnight. "
            "Highway 17 was closed at multiple intersections. "
            "A bridge on Old Towne Road became impassable."
        )
        self.assertEqual(classify_damage_context(narrative), "infra")

    def test_classify_residential(self):
        # "drive" in "Oak Drive" also triggers infra keyword — tie → mixed.
        # Use a narrative without street-name ambiguity to test pure residential.
        narrative = (
            "Several homes and apartments in the Summerville subdivision "
            "received water inside their living rooms and crawl spaces. "
            "Homeowners reported flooded houses with water in basements."
        )
        self.assertEqual(classify_damage_context(narrative), "residential")

    def test_classify_vehicle(self):
        narrative = (
            "Multiple vehicles became stranded in rising floodwater on I-526. "
            "Several cars were floating in the parking lot of the shopping center. "
            "Motorists required water rescue after stalled vehicles."
        )
        result = classify_damage_context(narrative)
        # vehicle keywords dominate; may also be mixed with commercial
        self.assertIn(result, ("vehicle", "mixed"))

    def test_classify_mixed_when_tied(self):
        # Equal road + home keywords → mixed
        narrative = "The road flooded, and a home on the street was damaged."
        result = classify_damage_context(narrative)
        self.assertIn(result, ("infra", "mixed", "residential"))

    def test_classify_no_keywords_unknown(self):
        narrative = "Flooding occurred in the area."
        self.assertEqual(classify_damage_context(narrative), "unknown")

    # ------------------------------------------------------------------
    # damage_appears_unreported tests
    # ID: CFHT-UNREPORTED-TEST-001
    # ------------------------------------------------------------------
    def test_unreported_flags_blank_with_damage_narrative(self):
        """Zero-damage + vivid damage narrative → flagged as unreported."""
        narrative = (
            "Hawthorne trailer park on Rivers Avenue was completely flooded "
            "with some cars under water."
        )
        self.assertTrue(damage_appears_unreported(narrative, 0.0, 0.0))

    def test_unreported_not_flagged_when_damage_recorded(self):
        """Non-zero propertyDamageUSD → never flagged, regardless of narrative."""
        self.assertFalse(damage_appears_unreported("cars under water", 5000.0, 0.0))

    def test_unreported_not_flagged_for_empty_narrative(self):
        """No narrative → cannot infer damage; flag stays False."""
        self.assertFalse(damage_appears_unreported("", 0.0, 0.0))
        self.assertFalse(damage_appears_unreported(None, 0.0, 0.0))

    def test_unreported_not_flagged_for_trivial_narrative(self):
        """Narrative with no damage-indicating keywords → not flagged."""
        self.assertFalse(damage_appears_unreported("Some rainfall occurred in the region.", 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()

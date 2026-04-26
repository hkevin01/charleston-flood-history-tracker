"""Unit tests for Charleston flood dataset helpers."""

import unittest

from scripts.build_dataset import (
    CITIES,
    FloodEvent,
    build_risk_zones,
    event_near_city,
    haversine_miles,
    parse_damage_to_usd,
    parse_float,
    parse_int,
)


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


if __name__ == "__main__":
    unittest.main()

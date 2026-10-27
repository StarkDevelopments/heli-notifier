"""Tests for the geometry helpers: distance, bearing, and the compass rose.

The haversine tests are anchored against known great-circle distances, because
a subtly wrong distance would move every alert radius decision with it.
"""

from __future__ import annotations

import math
import unittest

from heli_notifier.geo import compass_point, haversine_m, initial_bearing_deg

# Synthetic origin; real site coordinates live only in .env.
PIN_LAT = 0.0
PIN_LON = 0.0


class TestHaversine(unittest.TestCase):
    def test_zero_distance(self):
        self.assertAlmostEqual(haversine_m(PIN_LAT, PIN_LON, PIN_LAT, PIN_LON), 0.0, places=6)

    def test_known_great_circle(self):
        # One degree of longitude on the equator is about 111.2 km.
        dist = haversine_m(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(dist / 1000, 111.2, delta=0.1)

    def test_known_distance_at_mid_latitude(self):
        # One degree of longitude at 45 degrees latitude is about 78.6 km.
        dist = haversine_m(45.0, 0.0, 45.0, 1.0)
        self.assertAlmostEqual(dist / 1000, 78.6, delta=0.2)

    def test_symmetric(self):
        a = haversine_m(PIN_LAT, PIN_LON, 0.2, 0.3)
        b = haversine_m(0.2, 0.3, PIN_LAT, PIN_LON)
        self.assertAlmostEqual(a, b, places=6)

    def test_quarter_meridian(self):
        # A quarter of the way around the globe from the equator.
        d = haversine_m(0.0, 0.0, 0.0, 90.0)
        self.assertAlmostEqual(d / 1000, 10007.5, delta=1.0)

    def test_antipodal_is_finite(self):
        d = haversine_m(0.0, 0.0, 0.0, 180.0)
        self.assertTrue(math.isfinite(d))
        self.assertAlmostEqual(d / 1000, 20015.1, delta=1.0)


class TestBearing(unittest.TestCase):
    def test_due_north(self):
        self.assertAlmostEqual(initial_bearing_deg(PIN_LAT, PIN_LON, 52.0, PIN_LON), 0.0, places=3)

    def test_due_east(self):
        self.assertAlmostEqual(initial_bearing_deg(0.0, 0.0, 0.0, 1.0), 90.0, places=3)

    def test_due_south(self):
        self.assertAlmostEqual(initial_bearing_deg(52.0, PIN_LON, PIN_LAT, PIN_LON), 180.0, places=3)

    def test_due_west(self):
        self.assertAlmostEqual(initial_bearing_deg(0.0, 1.0, 0.0, 0.0), 270.0, places=3)

    def test_always_in_range(self):
        for lat, lon in [(0, 0), (12.3, -45.6), (-33.9, 151.2), (64.1, -21.9)]:
            b = initial_bearing_deg(PIN_LAT, PIN_LON, lat, lon)
            self.assertGreaterEqual(b, 0.0)
            self.assertLess(b, 360.0)


class TestCompass(unittest.TestCase):
    def test_cardinal_points(self):
        self.assertEqual(compass_point(0), "N")
        self.assertEqual(compass_point(90), "E")
        self.assertEqual(compass_point(180), "S")
        self.assertEqual(compass_point(270), "W")

    def test_intercardinal_points(self):
        self.assertEqual(compass_point(45), "NE")
        self.assertEqual(compass_point(135), "SE")
        self.assertEqual(compass_point(225), "SW")
        self.assertEqual(compass_point(315), "NW")

    def test_wraps_around_zero(self):
        self.assertEqual(compass_point(359), "N")
        self.assertEqual(compass_point(1), "N")

    def test_always_sixteen_distinct_values(self):
        points = {compass_point(b / 4.0) for b in range(0, 3600)}
        self.assertEqual(len(points), 16)


if __name__ == "__main__":
    unittest.main()

"""Tests for the adsb.fi client and its field coercion.

The synthetic fixture covers feed edge cases: `alt_baro` sent as the string
"ground", and an aircraft with a null position.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from datetime import timezone
from pathlib import Path
from unittest import mock

from heli_notifier.config import Config
from heli_notifier.sources.adsbfi import (
    ADSBFI_BASE_URL,
    AdsbFiClient,
    AdsbFiError,
    Aircraft,
    build_client,
    coerce_alt,
    coerce_float,
    iter_unique,
    normalise_callsign,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# An invented position for URL and fetch tests. Real coordinates are site data
# and live only in .env, never in the repository.
TEST_LAT, TEST_LON = 0.0, 0.0


def load_fixture():
    with open(FIXTURES / "adsbfi_v3_sample.json") as fh:
        return json.load(fh)


class FakeResponse:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestCoerceAlt(unittest.TestCase):
    def test_integer_passes_through(self):
        self.assertEqual(coerce_alt(31000), 31000)

    def test_float_is_truncated(self):
        self.assertEqual(coerce_alt(31000.7), 31000)

    def test_ground_string_becomes_none(self):
        # The trap: int("ground") raises ValueError and would abort a poll.
        self.assertIsNone(coerce_alt("ground"))

    def test_ground_string_is_case_and_space_insensitive(self):
        for value in ("ground", "GROUND", " ground ", "Ground"):
            self.assertIsNone(coerce_alt(value))

    def test_none_becomes_none(self):
        self.assertIsNone(coerce_alt(None))

    def test_empty_string_becomes_none(self):
        self.assertIsNone(coerce_alt(""))
        self.assertIsNone(coerce_alt("   "))

    def test_numeric_string_is_parsed(self):
        self.assertEqual(coerce_alt("12000"), 12000)
        self.assertEqual(coerce_alt(" 8500 "), 8500)

    def test_junk_string_becomes_none(self):
        for value in ("n/a", "none", "null", "-", "unknown", "12abc"):
            self.assertIsNone(coerce_alt(value))

    def test_booleans_are_rejected(self):
        # bool subclasses int, so True would otherwise become 1 foot.
        self.assertIsNone(coerce_alt(True))
        self.assertIsNone(coerce_alt(False))

    def test_zero_is_preserved(self):
        self.assertEqual(coerce_alt(0), 0)

    def test_never_raises(self):
        for value in [object(), [], {}, 3 + 4j, b"12000"]:
            self.assertIsNone(coerce_alt(value))


class TestCoerceFloat(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(coerce_float(441.25), 441.25)
        self.assertEqual(coerce_float(0), 0.0)

    def test_numeric_string(self):
        self.assertEqual(coerce_float("441.25"), 441.25)

    def test_bad_values(self):
        for value in [None, "", "abc", True, False, object()]:
            self.assertIsNone(coerce_float(value))


class TestNormaliseCallsign(unittest.TestCase):
    def test_trims_padding(self):
        self.assertEqual(normalise_callsign("  SAMPLE01  "), "SAMPLE01")

    def test_blank_becomes_none(self):
        self.assertIsNone(normalise_callsign("   "))
        self.assertIsNone(normalise_callsign(""))
        self.assertIsNone(normalise_callsign(None))

    def test_non_string_becomes_none(self):
        self.assertIsNone(normalise_callsign(12345))


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.payload = load_fixture()

    def _parse(self):
        with mock.patch("heli_notifier.sources.adsbfi.urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse(self.payload)
            return build_client(Config(lat=TEST_LAT, lon=TEST_LON)).fetch()

    def test_ground_aircraft_parsed_without_crashing(self):
        result = self._parse()
        by_hex = {a.icao24: a for a in result.aircraft}
        self.assertIn("a00001", by_hex)
        ground = by_hex["a00001"]
        self.assertIsNone(ground.baro_alt_ft)
        self.assertTrue(ground.on_ground)
        self.assertEqual(ground.callsign, "SAMPLE01")

    def test_aircraft_without_position_is_skipped(self):
        result = self._parse()
        self.assertNotIn("a00006", {a.icao24 for a in result.aircraft})

    def test_icao_normalised_to_lowercase(self):
        result = self._parse()
        for aircraft in result.aircraft:
            self.assertEqual(aircraft.icao24, aircraft.icao24.lower())

    def test_metadata_parsed(self):
        result = self._parse()
        self.assertEqual(result.total, 6)
        self.assertEqual(result.message, "No error")
        self.assertEqual(result.fetched_at.tzinfo, timezone.utc)

    def test_five_positioned_aircraft_from_six_entries(self):
        result = self._parse()
        self.assertEqual(len(result.aircraft), 5)
        self.assertEqual(result.total, 6)

    def test_optional_fields_absent_do_not_raise(self):
        result = self._parse()
        by_hex = {a.icao24: a for a in result.aircraft}
        # This entry has no alt_geom and no track.
        sample = by_hex["a00004"]
        self.assertIsNone(sample.geo_alt_ft)
        self.assertIsNone(sample.track_deg)
        self.assertEqual(sample.baro_alt_ft, 8500)

    def test_null_altitude_treated_as_ground(self):
        result = self._parse()
        glider = next(a for a in result.aircraft if a.icao24 == "a00003")
        self.assertIsNone(glider.baro_alt_ft)
        self.assertTrue(glider.on_ground)
        self.assertIsNone(glider.callsign)

    def test_mlat_flag_parsed(self):
        result = self._parse()
        by_hex = {a.icao24: a for a in result.aircraft}
        self.assertFalse(by_hex["a00002"].is_mlat)
        self.assertTrue(by_hex["a00005"].is_mlat)

    def test_emergency_and_squawk_parsed(self):
        result = self._parse()
        sample = next(a for a in result.aircraft if a.icao24 == "a00005")
        self.assertEqual(sample.squawk, "7700")
        self.assertEqual(sample.emergency, "7700")
        self.assertEqual(sample.description, "SAMPLE AIRCRAFT")

    def test_vertical_rate_signed(self):
        result = self._parse()
        by_hex = {a.icao24: a for a in result.aircraft}
        self.assertEqual(by_hex["a00002"].vertical_rate, -64.0)

    def test_altitude_zero_is_ground(self):
        self.assertTrue(Aircraft(icao24="x", lat=0, lon=0, baro_alt_ft=0).on_ground)

    def test_taxiing_with_altitude_is_airborne(self):
        # A 6 kt aircraft with a real altitude reading should not be treated
        # as parked on the ground.
        a = Aircraft(icao24="x", lat=0, lon=0, baro_alt_ft=1200, ground_speed_kt=6.0)
        self.assertFalse(a.on_ground)


class TestErrorHandling(unittest.TestCase):
    def _client(self):
        return AdsbFiClient("https://example.invalid/api", max_retries=2, backoff_base=0.01)

    def test_missing_ac_key_raises(self):
        with mock.patch("heli_notifier.sources.adsbfi.urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse({"total": 0, "msg": "No error"})
            with self.assertRaises(AdsbFiError):
                self._client().fetch()

    def test_ac_not_a_list_raises(self):
        with mock.patch("heli_notifier.sources.adsbfi.urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse({"ac": {"not": "a list"}})
            with self.assertRaises(AdsbFiError):
                self._client().fetch()

    def test_invalid_json_raises_after_retries(self):
        class BadResponse:
            status = 200

            def read(self):
                return b"not json at all"

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

        with mock.patch("heli_notifier.sources.adsbfi.urllib.request.urlopen") as urlopen:
            urlopen.return_value = BadResponse()
            with self.assertRaises(AdsbFiError):
                self._client().fetch()

    def test_client_error_is_not_retried(self):
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch("heli_notifier.sources.adsbfi.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(AdsbFiError):
                self._client().fetch()
        self.assertEqual(error.__class__, urllib.error.HTTPError)

    def test_server_error_is_retried(self):
        error = urllib.error.HTTPError("u", 503, "Unavailable", {}, None)
        with mock.patch(
            "heli_notifier.sources.adsbfi.urllib.request.urlopen", side_effect=error
        ) as urlopen:
            with self.assertRaises(AdsbFiError):
                self._client().fetch()
        self.assertEqual(urlopen.call_count, 3)

    def test_network_error_raises(self):
        with mock.patch(
            "heli_notifier.sources.adsbfi.urllib.request.urlopen",
            side_effect=urllib.error.URLError("no route"),
        ):
            with self.assertRaises(AdsbFiError):
                self._client().fetch()

    def test_recovers_on_a_later_attempt(self):
        good = load_fixture()
        ok = FakeResponse(good)
        bad = urllib.error.URLError("transient")
        with mock.patch("heli_notifier.sources.adsbfi.urllib.request.urlopen", side_effect=[bad, ok]):
            result = self._client().fetch()
        self.assertEqual(len(result.aircraft), 5)


class TestUrlBuilding(unittest.TestCase):
    def test_url_shape(self):
        url = Config(lat=TEST_LAT, lon=TEST_LON, radius_nm=54).url_for(ADSBFI_BASE_URL)
        self.assertEqual(url, "https://opendata.adsb.fi/api/v3/lat/0.00000/lon/0.00000/dist/54")

    def test_trailing_slash_tolerated(self):
        url = Config(lat=TEST_LAT, lon=TEST_LON).url_for("https://opendata.adsb.fi/api/")
        self.assertNotIn("//v3", url.split("://", 1)[1])

    def test_sends_identifiable_user_agent(self):
        with mock.patch("heli_notifier.sources.adsbfi.urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse(load_fixture())
            build_client(Config(lat=TEST_LAT, lon=TEST_LON)).fetch()
        request = urlopen.call_args[0][0]
        self.assertIn("heli-notifier", request.get_header("User-agent"))


class TestValidate(unittest.TestCase):
    """No default position: a missing pin must be named, not assumed."""

    def test_missing_position_is_named_not_assumed(self):
        problems = Config().validate()
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("latitude not set" in p for p in problems), problems)
        self.assertTrue(any("longitude not set" in p for p in problems), problems)

    def test_valid_position_passes(self):
        self.assertEqual(Config(lat=TEST_LAT, lon=TEST_LON).validate(), [])

    def test_out_of_range_position_is_rejected(self):
        problems = Config(lat=123.0, lon=TEST_LON).validate()
        self.assertIn("latitude 123.0 out of range", problems)

    def test_env_provides_the_position(self):
        import os

        env = {"HELI_NOTIFIER_LAT": "12.1", "HELI_NOTIFIER_LON": "-1.2"}
        with mock.patch.dict(os.environ, env):
            cfg = Config.from_env()
        self.assertEqual((cfg.lat, cfg.lon), (12.1, -1.2))


class TestIterUnique(unittest.TestCase):
    def test_collapses_duplicates_keeping_first(self):
        a = Aircraft(icao24="abc", lat=1, lon=1)
        b = Aircraft(icao24="abc", lat=2, lon=2)
        c = Aircraft(icao24="def", lat=3, lon=3)
        result = iter_unique([a, b, c])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].lat, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

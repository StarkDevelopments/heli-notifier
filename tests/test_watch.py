"""Tests for the emergency-services alerter.

The behaviours pinned here protect alert quality. In particular, getting closer
must not trigger a second alert, while separate aircraft sharing a callsign
must not create duplicate notifications for one incident.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import time
import unittest
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from heli_notifier.adsbdb import AdsbDbClient
from heli_notifier.alerts import (
    build_alert,
    closing_speed_kt,
    minutes_to_reach,
)
from heli_notifier.notify import LogNotifier, PushoverNotifier, build_notifier
from heli_notifier.sources.adsbfi import AdsbFiError, Aircraft, PollResult
from heli_notifier.watcher import Watcher
from heli_notifier.watchlist import LOCAL_SERVICE_LABELS, WatchConfig, classify

# An invented pin: tests need a fixed position, and real coordinates are site
# data that lives only in .env. Offsets below are latitude degrees (~111 km).
PIN_LAT = 0.0
PIN_LON = 0.0
T0 = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)


def craft(
    icao: str,
    *,
    lat: float = PIN_LAT,
    lon: float = PIN_LON,
    callsign: str | None = None,
    squawk: str | None = None,
    alt: int | None = 1500,
    gs: float | None = 80.0,
    track: float | None = None,
) -> Aircraft:
    return Aircraft(
        icao24=icao,
        lat=lat,
        lon=lon,
        callsign=callsign,
        baro_alt_ft=alt,
        ground_speed_kt=gs,
        track_deg=track,
        squawk=squawk,
    )


def watcher(**kwargs) -> Watcher:
    kwargs.setdefault("lat", PIN_LAT)
    kwargs.setdefault("lon", PIN_LON)
    return Watcher(config=WatchConfig(), dry_run=True, **kwargs)


class WatchlistTests(unittest.TestCase):
    def test_hems_and_police_prefixes_match(self):
        cfg = WatchConfig()
        self.assertIsNotNone(classify("HLE65", None, 5.0, cfg))
        self.assertIsNotNone(classify("HM67", None, 5.0, cfg))
        self.assertIsNotNone(classify("UKP01", None, 5.0, cfg))

    def test_unrelated_aircraft_does_not_match(self):
        cfg = WatchConfig()
        for callsign in ("BAW118", "EZY71LK", "G-ABCD", None, ""):
            self.assertIsNone(classify(callsign, None, 5.0, cfg), callsign)

    def test_hems_squawk_matches_without_a_callsign(self):
        cfg = WatchConfig()
        match = classify("BAW118", "0020", 5.0, cfg)
        self.assertIsNotNone(match)
        self.assertEqual(match.reason, "hems-squawk")

    def test_emergency_squawks_outrank_callsigns(self):
        cfg = WatchConfig()
        match = classify("BAW118", "7700", 5.0, cfg)
        self.assertIsNotNone(match)
        self.assertTrue(match.is_emergency)
        self.assertEqual(match.priority, 2)

    def test_emergency_squawk_wins_over_plain_hems(self):
        cfg = WatchConfig()
        match = classify("HLE22", "7500", 5.0, cfg)
        self.assertEqual(match.priority, 2)

    def test_squawk_0020_alone_is_not_an_emergency(self):
        cfg = WatchConfig()
        match = classify("HLE22", "0020", 5.0, cfg)
        self.assertFalse(match.is_emergency)

    def test_distance_only_affects_priority_not_membership(self):
        """An aircraft at 50 km is still a watched aircraft, just not urgent."""
        cfg = WatchConfig()
        far = classify("HLE22", None, 50.0, cfg)
        near = classify("HLE22", None, 5.0, cfg)
        self.assertIsNotNone(far)
        self.assertEqual(near.priority, 1)
        self.assertLess(far.priority, near.priority)

    def test_police_squawk_is_identified(self):
        cfg = WatchConfig()
        match = classify("BAW118", "0061", 5.0, cfg)
        self.assertIsNotNone(match)


class LabelTests(unittest.TestCase):
    def test_builtin_labels_resolve(self):
        cfg = WatchConfig()
        self.assertEqual(cfg.label_for("HLE22"), "Wiltshire and Bath Air Ambulance")
        self.assertEqual(cfg.label_for("HM67"), "Wales Air Ambulance")
        self.assertEqual(cfg.label_for("UKP01"), "National Police Air Service")

    def test_prefix_fallback_when_callsign_unknown(self):
        cfg = WatchConfig()
        self.assertEqual(cfg.label_for("HLE99"), "Air ambulance (HEMS)")
        self.assertEqual(cfg.label_for("UKP99"), "National Police Air Service")

    def test_unknown_callsign_has_no_label(self):
        self.assertEqual(WatchConfig().label_for("BAW118"), "Aircraft")
        self.assertEqual(WatchConfig().label_for(None), "Aircraft")

    def test_local_table_covers_the_watchlist(self):
        """Every local entry is a plausible designator, so typos are caught."""
        for callsign in LOCAL_SERVICE_LABELS:
            self.assertRegex(callsign, r"^(HLE|HM)\d+$")

    def test_label_file_merges_without_mutating_the_builtin_table(self):
        import json
        import tempfile

        before = dict(LOCAL_SERVICE_LABELS)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.json"
            path.write_text(json.dumps({"HLE99": "Test Air Ambulance"}))
            merged = WatchConfig().with_label_file(path)
        self.assertEqual(merged.label_for("HLE99"), "Test Air Ambulance")
        self.assertEqual(merged.label_for("HLE22"), "Wiltshire and Bath Air Ambulance")
        self.assertEqual(dict(LOCAL_SERVICE_LABELS), before)

    def test_label_file_rejects_non_object(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.json"
            path.write_text(json.dumps(["HLE99"]))
            with self.assertRaises(ValueError):
                WatchConfig().with_label_file(path)


class AlertMathTests(unittest.TestCase):
    def test_flying_straight_at_us_is_fully_closing(self):
        self.assertAlmostEqual(closing_speed_kt(100.0, 0.0, 0.0), 100.0, places=6)

    def test_flying_away_is_fully_negative(self):
        self.assertAlmostEqual(closing_speed_kt(100.0, 180.0, 0.0), -100.0, places=6)

    def test_crossing_is_near_zero(self):
        self.assertAlmostEqual(closing_speed_kt(100.0, 90.0, 0.0), 0.0, places=6)

    def test_missing_track_gives_none_not_zero(self):
        self.assertIsNone(closing_speed_kt(None, 90.0, 0.0))
        self.assertIsNone(closing_speed_kt(100.0, None, 0.0))

    def test_receding_aircraft_has_no_eta(self):
        self.assertIsNone(minutes_to_reach(10.0, -50.0))

    def test_closing_eta_uses_knots(self):
        minutes = minutes_to_reach(18.52, 60.0)
        self.assertAlmostEqual(minutes, 18.52 / (60.0 * 1.852) * 60.0, places=6)


class BuildAlertTests(unittest.TestCase):
    def alert(self, **kwargs):
        match = classify(kwargs.pop("callsign", "HLE22"), kwargs.pop("squawk", None),
                          kwargs.pop("distance_km", 5.0), WatchConfig())
        defaults = dict(
            icao24="406cbc", when=T0, distance_km=5.0, bearing_deg=225.0,
            baro_alt_ft=1500, ground_speed_kt=120.0, track_deg=45.0,
        )
        defaults.update(kwargs)
        return build_alert(match, **defaults)

    def test_message_names_the_operator(self):
        self.assertIn("Wiltshire and Bath Air Ambulance", self.alert().message)

    def test_title_carries_distance_and_wind(self):
        title = self.alert().title
        self.assertIn("HLE22", title)
        self.assertIn("5.0 km", title)
        self.assertIn("SW", title)

    def test_emergency_is_flagged_in_title_and_priority(self):
        alert = self.alert(callsign="BAW118", squawk="7700")
        self.assertTrue(alert.title.startswith("EMERGENCY"))
        self.assertEqual(alert.priority, 2)

    def test_no_line_starts_with_whitespace(self):
        """A leading space renders as a visibly ragged block on a phone."""
        for kwargs in (
            {"ground_speed_kt": 30.0, "track_deg": 45.0},
            {"ground_speed_kt": 120.0, "track_deg": 45.0},
            {"ground_speed_kt": 73.0, "track_deg": 225.0},
        ):
            for line in self.alert(**kwargs).message.splitlines():
                self.assertEqual(line, line.lstrip(), line)

    def test_registration_and_type_are_included_when_known(self):
        message = self.alert(registration="G-WOAC", type_name="EC135").message
        self.assertIn("G-WOAC", message)
        self.assertIn("EC135", message)

    def test_squawk_corroborating_a_callsign_is_shown(self):
        self.assertIn("squawk 0061", self.alert(callsign="UKP01", squawk="0061").message)

    def test_squawk_is_not_repeated_when_it_caused_the_match(self):
        alert = self.alert(callsign="BAW118", squawk="0020")
        self.assertNotIn("squawk", alert.message)

    def test_squawk_line_describes_the_code_not_the_match(self):
        """An HLE aircraft on a police code must not be labelled 'HEMS' there."""
        message = self.alert(callsign="HLE22", squawk="0061").message
        self.assertIn("squawk 0061 (police)", message)
        self.assertNotIn("(HEMS designator)", message)

    def test_unknown_distress_squawk_names_the_situation(self):
        message = self.alert(callsign="BAW118", squawk="7700").message
        self.assertIn("Distress squawk 7700", message)


class WatcherStateTests(unittest.TestCase):
    def test_entry_inside_radius_alerts_once(self):
        w = watcher()
        first = w.poll_once(when=T0, aircraft=[craft("406cbc", callsign="HLE22")])
        second = w.poll_once(when=T0 + timedelta(seconds=30), aircraft=[craft("406cbc", callsign="HLE22")])
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    def test_first_sighting_outside_entry_radius_does_not_alert(self):
        """Seen for the first time at 22 km: not an arrival worth alerting."""
        w = watcher()
        outside = craft("406cbc", callsign="HLE22", lat=PIN_LAT + 0.197)
        self.assertEqual(w.poll_once(when=T0, aircraft=[outside]), [])

    def test_entry_boundary_is_the_configured_radius(self):
        w = watcher()
        just_inside = craft("406cbc", callsign="HLE22", lat=PIN_LAT + 0.088)
        found = w.evaluate(just_inside)
        self.assertIsNotNone(found)
        match, distance_km, _, within_entry = found
        self.assertTrue(within_entry)
        self.assertLessEqual(distance_km, 10.0)

    def test_exit_hysteresis_keeps_aircraft_tracked_past_the_radius(self):
        """Between the two radii it stays tracked, so no re-alert on the way out."""
        w = watcher()
        self.assertEqual(len(w.poll_once(when=T0, aircraft=[craft("406cbc", callsign="HLE22")])), 1)
        between = craft("406cbc", callsign="HLE22", lat=PIN_LAT + 0.10)  # ~11.1 km
        found = w.evaluate(between)
        self.assertIsNotNone(found)
        self.assertFalse(found[3], "should be outside the entry radius")
        self.assertEqual(
            w.poll_once(when=T0 + timedelta(seconds=30), aircraft=[between]), []
        )
        self.assertIn("406cbc", w._tracked)

    def test_closing_does_not_re_alert(self):
        """The regression that doubled every alert: closing 9 km -> 4 km is not news."""
        w = watcher()
        far = craft("406cbc", callsign="HLE22", lat=PIN_LAT + 0.08)
        near = craft("406cbc", callsign="HLE22", lat=PIN_LAT + 0.04)
        self.assertEqual(len(w.poll_once(when=T0, aircraft=[far])), 1)
        self.assertEqual(
            w.poll_once(when=T0 + timedelta(seconds=30), aircraft=[near]), [],
            "an aircraft getting closer must not re-alert",
        )

    def test_departure_and_return_alerts_again(self):
        w = watcher()
        near = craft("406cbc", callsign="HLE22")
        away = craft("406cbc", callsign="HLE22", lat=PIN_LAT + 0.5)
        self.assertEqual(len(w.poll_once(when=T0, aircraft=[near])), 1)
        for i in range(8):
            w.poll_once(when=T0 + timedelta(seconds=30 * (i + 1)), aircraft=[away])
        self.assertNotIn("406cbc", w._tracked)
        self.assertEqual(
            len(w.poll_once(when=T0 + timedelta(seconds=600), aircraft=[near])), 1
        )

    def test_one_missing_poll_does_not_forget_the_aircraft(self):
        """Signal loss is not a departure; forgetting here would re-alert."""
        w = watcher()
        near = craft("406cbc", callsign="HLE22")
        w.poll_once(when=T0, aircraft=[near])
        w.poll_once(when=T0 + timedelta(seconds=30), aircraft=[])
        self.assertEqual(w.poll_once(when=T0 + timedelta(seconds=60), aircraft=[near]), [])

    def test_two_aircraft_sharing_a_callsign_alert_once(self):
        """Two HLE65 airframes in formation are one incident, not two phones."""
        w = watcher()
        alerts = w.poll_once(
            when=T0,
            aircraft=[
                craft("405f81", callsign="HLE65", lat=PIN_LAT + 0.01),
                craft("407685", callsign="HLE65", lat=PIN_LAT + 0.011),
            ],
        )
        self.assertEqual(len(alerts), 1)

    def test_emergency_escalation_always_gets_through(self):
        w = watcher()
        w.poll_once(when=T0, aircraft=[craft("406cbc", callsign="HLE22")])
        escalating = craft("406cbc", callsign="HLE22", squawk="7700")
        alerts = w.poll_once(when=T0 + timedelta(seconds=30), aircraft=[escalating])
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0].is_emergency)

    def test_emergency_is_never_suppressed_by_the_duplicate_window(self):
        w = watcher()
        w.poll_once(when=T0, aircraft=[craft("406cbc", callsign="HLE22")])
        # Another airframe, same callsign, squawking distress one minute later.
        other = craft("407685", callsign="HLE22", squawk="7700")
        alerts = w.poll_once(when=T0 + timedelta(seconds=60), aircraft=[other])
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0].is_emergency)

    def test_aircraft_on_the_ground_is_ignored(self):
        """A parked air ambulance wears a watched callsign and must stay silent."""
        w = watcher()
        parked = craft("406cbc", callsign="HLE22", alt=None, gs=0.0)
        self.assertTrue(parked.on_ground)
        self.assertEqual(w.poll_once(when=T0, aircraft=[parked]), [])
        self.assertEqual(w.stats.ground_skipped, 1)

    def test_low_altitude_hovering_aircraft_still_alerts(self):
        """50 ft over a hospital is the case worth waking up for."""
        w = watcher()
        hovering = craft("406cbc", callsign="HLE22", alt=50, gs=2.0)
        self.assertEqual(len(w.poll_once(when=T0, aircraft=[hovering])), 1)

    def test_loiter_reminder_fires_after_the_interval(self):
        w = watcher(loiter_reminder_seconds=3600.0)
        near = craft("406cbc", callsign="HLE22")
        self.assertEqual(len(w.poll_once(when=T0, aircraft=[near])), 1)
        later = w.poll_once(when=T0 + timedelta(seconds=3700), aircraft=[near])
        self.assertEqual(len(later), 1)
        self.assertEqual(
            w.poll_once(when=T0 + timedelta(seconds=3800), aircraft=[near]), [],
            "the reminder must not repeat every poll",
        )

    def test_failed_poll_does_not_clear_tracking(self):
        """A network blip must not be mistaken for the aircraft leaving."""
        w = watcher()
        w.poll_once(when=T0, aircraft=[craft("406cbc", callsign="HLE22")])

        class Boom:
            def fetch(self):
                from heli_notifier.sources.adsbfi import AdsbFiError

                raise AdsbFiError("network down")

        w.bind(Boom())
        w.poll_once(when=T0 + timedelta(seconds=30))
        self.assertIn("406cbc", w._tracked)
        self.assertEqual(w.stats.polls_failed, 1)


class _EmptyFeed:
    """A feed that answers successfully with nothing overhead."""

    def fetch(self) -> PollResult:
        return PollResult(fetched_at=datetime.now(timezone.utc), aircraft=[])


class _DeadFeed:
    """A feed that fails every poll, as a network outage would."""

    def fetch(self) -> PollResult:
        raise AdsbFiError("network down")


class HeartbeatTests(unittest.TestCase):
    """The loop leaves a stamp so an external probe can see it turning.

    The gap this closes: `restart: unless-stopped` restarts a process that
    *exited*, but nothing notices a process that is alive and no longer
    looping. The watcher writes a heartbeat after every poll, and the
    container's healthcheck reads it.
    """

    def test_run_writes_a_fresh_heartbeat(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat"
            w = Watcher(
                lat=PIN_LAT,
                lon=PIN_LON,
                config=replace(WatchConfig(), interval_seconds=7),
                dry_run=True,
                heartbeat_path=path,
            )
            w.bind(_EmptyFeed())
            w.run(once=True)
            stamp_text, interval_text = path.read_text().split()
            self.assertLess(abs(time.time() - float(stamp_text)), 5)
            self.assertEqual(float(interval_text), 7.0)

    def test_a_failed_poll_still_writes_the_heartbeat(self):
        """The probe measures the loop, not the feed: an outage is not a hang."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat"
            w = watcher(heartbeat_path=path)
            w.bind(_DeadFeed())
            w.run(once=True)
            self.assertTrue(path.exists())
            self.assertEqual(w.stats.polls_failed, 1)

    def test_heartbeat_writing_can_be_disabled(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat"
            w = watcher(heartbeat_path=None)
            w.bind(_EmptyFeed())
            w.run(once=True)
            self.assertFalse(path.exists())

    def test_healthcheck_passes_on_a_fresh_heartbeat(self):
        from heli_notifier.cli import cmd_healthcheck

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat"
            path.write_text(f"{time.time():.3f} 30\n")
            with mock.patch("heli_notifier.cli.HEARTBEAT_PATH", path):
                rc = cmd_healthcheck(argparse.Namespace(max_age=None))
        self.assertEqual(rc, 0)

    def test_healthcheck_fails_on_a_stale_heartbeat(self):
        from heli_notifier.cli import cmd_healthcheck

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat"
            path.write_text(f"{time.time() - 400:.3f} 30\n")
            with mock.patch("heli_notifier.cli.HEARTBEAT_PATH", path):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    rc = cmd_healthcheck(argparse.Namespace(max_age=None))
        self.assertEqual(rc, 1)
        self.assertIn("unhealthy", err.getvalue())

    def test_healthcheck_fails_without_a_heartbeat(self):
        """No stamp is unhealthy, not healthy-by-default: a fresh container
        that never polls must not look alive."""
        from heli_notifier.cli import cmd_healthcheck

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat"
            with mock.patch("heli_notifier.cli.HEARTBEAT_PATH", path):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = cmd_healthcheck(argparse.Namespace(max_age=None))
        self.assertEqual(rc, 1)

    def test_healthcheck_max_age_override_tightens_the_threshold(self):
        from heli_notifier.cli import cmd_healthcheck

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat"
            path.write_text(f"{time.time() - 40:.3f} 30\n")
            with mock.patch("heli_notifier.cli.HEARTBEAT_PATH", path):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = cmd_healthcheck(argparse.Namespace(max_age=10.0))
        self.assertEqual(rc, 1)


class NotifierTests(unittest.TestCase):
    def test_log_notifier_reports_success_and_has_a_name(self):
        notifier = LogNotifier()
        self.assertTrue(notifier.send(self._alert()))
        self.assertEqual(notifier.failed, 0)
        self.assertTrue(notifier.name)

    def test_build_notifier_falls_back_to_log_without_credentials(self):
        self.assertIsInstance(build_notifier(None, None), LogNotifier)
        self.assertIsInstance(build_notifier("", ""), LogNotifier)

    def test_build_notifier_uses_pushover_when_configured(self):
        self.assertIsInstance(build_notifier("token", "key"), PushoverNotifier)

    def test_partial_credentials_are_an_error(self):
        """Half a configuration must fail loudly, not silently log instead."""
        with self.assertRaises(ValueError):
            build_notifier("token", None)

    def test_user_key_env_name_is_canonical(self):
        """The .env spelling is PUSHOVER_USER_KEY."""
        from heli_notifier.cli import _pushover_credentials

        with mock.patch.dict(os.environ, {"PUSHOVER_TOKEN": "t", "PUSHOVER_USER_KEY": "u"},
                             clear=True):
            self.assertEqual(_pushover_credentials(), ("t", "u"))

    def test_legacy_key_env_name_is_not_read(self):
        """PUSHOVER_KEY is deliberately not an alias any more.

        It must leave the user key unset, so setting it now produces the same
        loud "partly configured" error as setting nothing at all, rather than
        quietly reviving a name the docs no longer mention.
        """
        from heli_notifier.cli import _pushover_credentials

        with mock.patch.dict(os.environ, {"PUSHOVER_TOKEN": "t", "PUSHOVER_KEY": "u"},
                             clear=True):
            token, user_key = _pushover_credentials()
            self.assertEqual(token, "t")
            self.assertIsNone(user_key)
            with self.assertRaises(ValueError):
                build_notifier(token, user_key)

    def test_wrong_spelling_is_caught_rather_than_ignored(self):
        """A user key set under a typo'd name must not look like 'no pushover'.

        This is the whole reason the env var is read in one place: setting
        PUSHOVER_USER while the code looks for PUSHOVER_USER_KEY would
        otherwise downgrade a live alerter to log-only and never say so.
        """
        from heli_notifier.cli import _pushover_credentials

        with mock.patch.dict(os.environ, {"PUSHOVER_TOKEN": "t", "PUSHOVER_USER": "u"},
                             clear=True):
            token, user_key = _pushover_credentials()
            self.assertEqual(token, "t")
            self.assertIsNone(user_key)
            # i.e. build_notifier would raise rather than silently log.
            with self.assertRaises(ValueError):
                build_notifier(token, user_key)

    def test_placeholder_from_env_example_is_treated_as_no_credentials(self):
        """An unfilled .env must not become a notifier that silently 401s."""
        from heli_notifier.notify import is_placeholder

        self.assertTrue(is_placeholder("replace-with-your-app-token"))
        self.assertTrue(is_placeholder("replace-with-your-user-key"))
        self.assertTrue(is_placeholder("  "))
        self.assertTrue(is_placeholder(None))
        self.assertFalse(is_placeholder("aH72bXk3Qz9dL7mN"))

        both = build_notifier("replace-with-your-app-token", "replace-with-your-user-key")
        self.assertIsInstance(both, LogNotifier)

    def test_half_filled_env_template_is_an_error(self):
        """One real value plus one placeholder must fail, not fall back to log."""
        with self.assertRaises(ValueError) as ctx:
            build_notifier("aH72bXk3Qz9dL7mN", "replace-with-your-user-key")
        self.assertIn("user key", str(ctx.exception))

    def test_verify_accepts_a_good_response(self):
        with mock.patch("heli_notifier.notify.urllib.request.urlopen",
                        return_value=self._ok_response({"status": 1, "user": "valid"})):
            ok, message = build_notifier("token", "key").verify()
        self.assertTrue(ok, message)

    def test_verify_rejects_bad_credentials_with_the_reason(self):
        with mock.patch("heli_notifier.notify.urllib.request.urlopen",
                        return_value=self._ok_response(
                            {"user": "valid", "status": 0,
                             "errors": ["user identifier is invalid"]})):
            ok, message = build_notifier("token", "key").verify()
        self.assertFalse(ok)
        self.assertIn("user identifier is invalid", message)

    def test_verify_reports_unreachable_network(self):
        with mock.patch("heli_notifier.notify.urllib.request.urlopen",
                        side_effect=OSError("no route to host")):
            ok, message = build_notifier("token", "key").verify()
        self.assertFalse(ok)
        self.assertIn("could not reach", message)

    def test_paused_pushover_is_reported(self):
        notifier = build_notifier("token", "key")
        notifier._urlopen = lambda *a, **k: self._response(599)
        self.assertFalse(notifier.send(self._alert()))

    def test_network_error_is_reported_not_raised(self):
        notifier = build_notifier("token", "key")

        def boom(*a, **k):
            raise OSError("no route to host")

        notifier._urlopen = boom
        self.assertFalse(notifier.send(self._alert()))
        self.assertEqual(notifier.failed, 1)

    @staticmethod
    def _response(status: int):
        import io
        import urllib.error

        return urllib.error.HTTPError("u", status, "err", {}, io.BytesIO(b""))

    @staticmethod
    def _ok_response(body: dict):
        """A minimal stand-in for the object urlopen returns on a 200."""

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(body).encode("utf-8")

        return _Resp()

    @staticmethod
    def _alert(priority: int = 0):
        from heli_notifier.alerts import build_alert

        match = classify("HLE22", "7700", 5.0, WatchConfig())
        match = replace(match, priority=priority)
        return build_alert(match, icao24="406cbc", when=T0, distance_km=5.0, bearing_deg=225.0)

    @staticmethod
    def _fields(notifier, alert) -> dict[str, str]:
        """Decode the form body the notifier would POST."""
        raw = notifier._payload(alert).decode("utf-8")
        return dict(urllib.parse.parse_qsl(raw))

    def test_siren_is_the_default_sound(self):
        fields = self._fields(build_notifier("t", "k"), self._alert())
        self.assertEqual(fields["sound"], "siren")

    def test_sound_can_be_overridden_and_silenced(self):
        quiet = build_notifier("t", "k", sound="none")
        self.assertEqual(self._fields(quiet, self._alert())["sound"], "none")
        plain = build_notifier("t", "k", sound="")
        self.assertNotIn("sound", self._fields(plain, self._alert()))

    def test_every_alert_carries_the_twelve_hour_ttl(self):
        for priority in (0, 1, 2):
            with self.subTest(priority=priority):
                fields = self._fields(build_notifier("t", "k"), self._alert(priority))
                self.assertEqual(fields["ttl"], "43200")
                self.assertEqual(fields["ttl"], str(12 * 3600))

    def test_no_alert_ever_repeats(self):
        """Nothing re-notifies, including a distress squawk.

        Pushover repeats a priority-2 message until acknowledged, capped at 3
        hours and 50 retries. A siren every few minutes for three hours is how a
        notification gets muted, so no alert may put the alerter in that mode.
        """
        for priority in (0, 1, 2):
            with self.subTest(priority=priority):
                fields = self._fields(build_notifier("t", "k"), self._alert(priority))
                self.assertNotIn("retry", fields)
                self.assertNotIn("expire", fields)

    def test_wire_priority_is_uniform_and_never_emergency(self):
        """Same priority for every alert: normal (0), never emergency (2).

        The alerter's own priority still describes the event, but it does not
        reach the wire. Priority 0 is a deliberate choice: Pushover downgrades
        a normal-priority notification to silence during the recipient's quiet
        hours, and that is the user's own setting to control, not something the
        alerter should override for every alert.
        """
        seen = set()
        for priority in (0, 1, 2):
            fields = self._fields(build_notifier("t", "k"), self._alert(priority))
            seen.add(fields["priority"])
            self.assertNotEqual(fields["priority"], "2", "priority 2 repeats until acknowledged")
            self.assertNotEqual(fields["priority"], "1", "priority 1 is not the agreed wire value")
        self.assertEqual(seen, {"0"})

    def test_distress_alert_keeps_its_own_wording(self):
        """Uniform transport does not mean losing the distress signal.

        The alerter still suppresses duplicates for a distress squawk and still
        titles it, so the distinction stays visible where it is useful.
        """
        alert = self._alert(2)
        self.assertTrue(alert.is_emergency)
        self.assertTrue(alert.title.startswith("EMERGENCY - "))
        self.assertIn("Distress squawk 7700", alert.message)

    def test_shorter_ttl_is_honoured(self):
        fields = self._fields(build_notifier("t", "k", ttl_seconds=900), self._alert())
        self.assertEqual(fields["ttl"], "900")

    def test_ttl_env_is_read_in_hours(self):
        from heli_notifier.cli import _pushover_ttl

        with mock.patch.dict(os.environ, {"PUSHOVER_TTL_HOURS": "12"}, clear=True):
            self.assertEqual(_pushover_ttl(), 43200)
        with mock.patch.dict(os.environ, {"PUSHOVER_TTL_HOURS": "0.5"}, clear=True):
            self.assertEqual(_pushover_ttl(), 1800)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_pushover_ttl(), 43200)

    def test_bad_ttl_env_is_rejected_loudly(self):
        from heli_notifier.cli import _pushover_ttl

        with mock.patch.dict(os.environ, {"PUSHOVER_TTL_HOURS": "forever"}, clear=True):
            with self.assertRaises(ValueError):
                _pushover_ttl()

    def test_failed_sends_are_visible_in_the_health_line(self):
        """A rejected notification must not be invisible on a quiet day.

        The alerter runs for weeks and prints this one line every 20 polls. If
        a failed send is not in it, a revoked token or an exhausted quota looks
        exactly like a healthy run.
        """
        from heli_notifier.watcher import WatchStats

        healthy = WatchStats(polls_attempted=20, alerts_raised=3)
        self.assertNotIn("FAILURE", healthy.summary())
        self.assertIn("3 alerts", healthy.summary())

        broken = WatchStats(polls_attempted=20, alerts_raised=3, notify_failures=2)
        self.assertIn("2 NOTIFICATION FAILURES", broken.summary())

    def test_invalid_ttl_is_refused(self):
        with self.assertRaises(ValueError):
            build_notifier("t", "k", ttl_seconds=0)
        with self.assertRaises(ValueError):
            build_notifier("t", "k", ttl_seconds=-1)

    def test_test_notification_uses_a_real_newline(self):
        """The TEST marker must be a line break, not the two characters \\n.

        Shipping "TEST\\n" in source produces a literal backslash and n, which
        arrives on the phone as visible "\\n". Assert on the built message, not
        on the source text, so a re-escape cannot pass unnoticed.
        """
        from heli_notifier.cli import cmd_notify_test

        sent: list = []
        capture = _CaptureNotifier(sent)

        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("heli_notifier.cli.build_notifier", return_value=capture):
                self.assertEqual(cmd_notify_test(argparse.Namespace(device=None)), 0)

        self.assertEqual(len(sent), 1)
        message = sent[0].message
        self.assertTrue(message.startswith("TEST\n"), repr(message[:20]))
        self.assertNotIn("\\n", message, "message contains a literal backslash-n")
        self.assertIn("\nWiltshire and Bath Air Ambulance", message)
        self.assertTrue(sent[0].title.startswith("TEST - "))

    def test_test_notification_refuses_without_real_credentials(self):
        """No credentials must be a refusal, not a fake success.

        Otherwise a placeholder .env would print "sent" and imply the phone was
        proven when nothing left the machine.
        """
        from heli_notifier.cli import cmd_notify_test

        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.dict(os.environ, {}, clear=True):
                rc = cmd_notify_test(argparse.Namespace(device=None))
        self.assertEqual(rc, 2)

    def test_watch_rejects_a_non_numeric_environment(self):
        """A malformed position must be an error message, not a traceback."""
        from heli_notifier.cli import cmd_watch

        args = argparse.Namespace(
            lat=None, lon=None, radius=None, data_dir=None, alert_radius=None,
            interval=None, labels=None, device=None, log_level="CRITICAL",
            once=True, dry_run=True, no_lookup=True,
        )
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with mock.patch.dict(os.environ, {"HELI_NOTIFIER_LAT": "abc"}, clear=True):
                rc = cmd_watch(args)
        self.assertEqual(rc, 2)
        self.assertIn("not a valid number", err.getvalue())

    def test_test_notification_shows_a_closing_aircraft(self):
        """A receding test alert has no time-to-overhead, which is the least
        useful shape to confirm you can read on a phone at night."""
        from heli_notifier.cli import cmd_notify_test

        sent: list = []
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("heli_notifier.cli.build_notifier",
                            return_value=_CaptureNotifier(sent)):
                cmd_notify_test(argparse.Namespace(device=None))

        self.assertIn("closing", sent[0].message)
        self.assertIn("to overhead", sent[0].message)


    def test_placeholder_from_env_example_is_treated_as_no_credentials(self):
        """An unfilled .env must not become a notifier that silently 401s."""
        from heli_notifier.notify import is_placeholder

        self.assertTrue(is_placeholder("replace-with-your-app-token"))
        self.assertTrue(is_placeholder("replace-with-your-user-key"))
        self.assertTrue(is_placeholder("  "))
        self.assertTrue(is_placeholder(None))
        self.assertFalse(is_placeholder("aH72bXk3Qz9dL7mN"))

        both = build_notifier("replace-with-your-app-token", "replace-with-your-user-key")
        self.assertIsInstance(both, LogNotifier)

    def test_half_filled_env_template_is_an_error(self):
        """One real value plus one placeholder must fail, not fall back to log."""
        with self.assertRaises(ValueError) as ctx:
            build_notifier("aH72bXk3Qz9dL7mN", "replace-with-your-user-key")
        self.assertIn("user key", str(ctx.exception))

    def test_verify_accepts_a_good_response(self):
        with mock.patch("heli_notifier.notify.urllib.request.urlopen",
                        return_value=self._ok_response({"status": 1, "user": "valid"})):
            ok, message = build_notifier("token", "key").verify()
        self.assertTrue(ok, message)

    def test_verify_rejects_bad_credentials_with_the_reason(self):
        with mock.patch("heli_notifier.notify.urllib.request.urlopen",
                        return_value=self._ok_response(
                            {"user": "valid", "status": 0,
                             "errors": ["user identifier is invalid"]})):
            ok, message = build_notifier("token", "key").verify()
        self.assertFalse(ok)
        self.assertIn("user identifier is invalid", message)

    def test_verify_reports_unreachable_network(self):
        with mock.patch("heli_notifier.notify.urllib.request.urlopen",
                        side_effect=OSError("no route to host")):
            ok, message = build_notifier("token", "key").verify()
        self.assertFalse(ok)
        self.assertIn("could not reach", message)

    def test_paused_pushover_is_reported(self):
        notifier = build_notifier("token", "key")
        notifier._urlopen = lambda *a, **k: self._response(599)
        self.assertFalse(notifier.send(self._alert()))

    def test_network_error_is_reported_not_raised(self):
        notifier = build_notifier("token", "key")

        def boom(*a, **k):
            raise OSError("no route to host")

        notifier._urlopen = boom
        self.assertFalse(notifier.send(self._alert()))
        self.assertEqual(notifier.failed, 1)

    @staticmethod
    def _response(status: int):
        import io
        import urllib.error

        return urllib.error.HTTPError("u", status, "err", {}, io.BytesIO(b""))

    @staticmethod
    def _ok_response(body: dict):
        """A minimal stand-in for the object urlopen returns on a 200."""

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(body).encode("utf-8")

        return _Resp()

    @staticmethod
    def _alert(priority: int = 0):
        from heli_notifier.alerts import build_alert

        match = classify("HLE22", "7700", 5.0, WatchConfig())
        match = replace(match, priority=priority)
        return build_alert(match, icao24="406cbc", when=T0, distance_km=5.0, bearing_deg=225.0)

    @staticmethod
    def _fields(notifier, alert) -> dict[str, str]:
        """Decode the form body the notifier would POST."""
        raw = notifier._payload(alert).decode("utf-8")
        return dict(urllib.parse.parse_qsl(raw))

    def test_siren_is_the_default_sound(self):
        fields = self._fields(build_notifier("t", "k"), self._alert())
        self.assertEqual(fields["sound"], "siren")

    def test_sound_can_be_overridden_and_silenced(self):
        quiet = build_notifier("t", "k", sound="none")
        self.assertEqual(self._fields(quiet, self._alert())["sound"], "none")
        plain = build_notifier("t", "k", sound="")
        self.assertNotIn("sound", self._fields(plain, self._alert()))


class _CaptureNotifier:
    """A notifier that records alerts instead of sending them.

    Lives at module level so the notify-test tests do not each redefine it, and
    implements `verify()` because the command under test calls it before it
    would ever send anything.
    """

    name = "capture"

    def __init__(self, sent: list) -> None:
        self.sent = sent
        self.sent_count = 0
        self.failed = 0

    def verify(self, **kwargs):
        return True, "capture: credentials not checked"

    def send(self, alert) -> bool:
        self.sent.append(alert)
        self.sent_count += 1
        return True

    def close(self) -> None:
        return None


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class AdsbDbTests(unittest.TestCase):
    PAYLOAD = {
        "type": "Helicopter",
        "manufacturer": "AIRBUS",
        "mode_s": "406cbc",
        "registration": "G-WOAC",
        "registered_owner_country_iso_name": "United Kingdom",
        "registered_owner_operator_flag_code": "B",
    }

    def client(self, tmp, **kwargs):
        import tempfile

        tmpdir = tempfile.mkdtemp(dir=tmp)
        path = Path(tmpdir) / "cache.json"
        self.addCleanup(lambda: None)
        calls: list[str] = []

        def fetch(key: str):
            calls.append(key)
            return dict(self.PAYLOAD) if key.lower() in ("g-woac", "406cbc") else None

        client = AdsbDbClient(
            path, fetch=fetch, sleep=lambda _s: None, **kwargs
        )
        return client, calls

    def test_lookup_returns_registration_and_type(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, calls = self.client(tmp)
            identity = client.identity("406cbc")
        self.assertEqual(identity.registration, "G-WOAC")
        self.assertIn("Helicopter", identity.type_name)
        self.assertEqual(calls, ["406cbc"])

    def test_second_lookup_is_served_from_cache(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, calls = self.client(tmp)
            client.identity("406cbc")
            client.identity("406cbc")
            self.assertEqual(len(calls), 1)
            self.assertEqual(client.hits, 1)

    def test_cache_survives_a_restart_and_stays_fresh(self):
        """The stamp must be wall-clock, or a restart makes it unreadable."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(1_000_000.0)
            path = Path(tmp) / "cache.json"
            first = AdsbDbClient(
                path, fetch=lambda k: dict(self.PAYLOAD),
                sleep=lambda _s: None, wall_clock=clock,
            )
            first.identity("406cbc")

            # A new process: the monotonic clock starts over from near zero,
            # which is exactly what makes a persisted monotonic stamp wrong.
            later = FakeClock(1_000_100.0)
            second = AdsbDbClient(
                path, fetch=lambda k: (_ for _ in ()).throw(AssertionError("refetched")),
                sleep=lambda _s: None, wall_clock=later,
            )
            identity = second.identity("406cbc")
            self.assertEqual(identity.registration, "G-WOAC")

    def test_stale_entry_is_refetched(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            client, calls = self.client(
                tmp, ttl_seconds=3600.0, wall_clock=clock
            )
            client.identity("406cbc")
            clock.advance(3601.0)
            client.identity("406cbc")
            self.assertEqual(len(calls), 2)

    def test_future_stamp_is_treated_as_stale(self):
        """A clock jump must not pin an entry as fresh forever."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(1_000_000.0)
            client, calls = self.client(tmp, ttl_seconds=3600.0, wall_clock=clock)
            client.identity("406cbc")
            clock.value -= 7200.0  # wall clock moved backwards
            client.identity("406cbc")
            self.assertEqual(len(calls), 2)

    def test_unknown_key_returns_none(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, _ = self.client(tmp)
            self.assertIsNone(client.identity("nonexistent"))

    def test_lookup_failure_never_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            def boom(key):
                raise OSError("network down")

            client = AdsbDbClient(
                Path(tempfile.mkdtemp(dir=tmp)) / "c.json",
                fetch=boom, sleep=lambda _s: None,
            )
            self.assertIsNone(client.identity("406cbc"))
            self.assertEqual(client.failures, 1)

    def test_stale_answer_is_served_when_the_refresh_fails(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            state = {"ok": True}

            def fetch(key):
                if not state["ok"]:
                    return None
                return dict(self.PAYLOAD)

            path = Path(tmp) / "c.json"
            client = AdsbDbClient(
                path, fetch=fetch, sleep=lambda _s: None,
                ttl_seconds=100.0, wall_clock=clock,
            )
            client.identity("406cbc")
            clock.advance(200.0)
            state["ok"] = False
            identity = client.identity("406cbc")
            self.assertIsNotNone(identity)
            self.assertEqual(identity.registration, "G-WOAC")

    def test_cache_key_is_case_insensitive(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, calls = self.client(tmp)
            client.identity("G-WOAC")
            client.identity("g-woac")
            self.assertEqual(len(calls), 1)

    def test_corrupt_cache_is_ignored(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text("{not json")
            client = AdsbDbClient(
                path, fetch=lambda k: dict(self.PAYLOAD), sleep=lambda _s: None
            )
            self.assertEqual(client.identity("406cbc").registration, "G-WOAC")


class SequenceTests(unittest.TestCase):
    """Drive the state machine through scripted sequences of positions."""

    def _fly_in(self, w: Watcher, offsets=(0.35, 0.25, 0.15, 0.05)):
        """One air ambulance closing from ~39 km to the centre over four polls."""
        alerts = []
        for i, offset in enumerate(offsets):
            when = T0 + timedelta(seconds=30 * i)
            alerts.extend(
                w.poll_once(
                    when=when,
                    aircraft=[
                        craft("406cbc", lat=PIN_LAT + offset, callsign="HLE22", track=180.0)
                    ],
                )
            )
        return alerts

    def test_a_closing_arrival_alerts_exactly_once(self):
        w = watcher()
        alerts = self._fly_in(w)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].callsign, "HLE22")

    def test_the_first_sample_inside_the_radius_is_the_alert(self):
        """The first poll inside 10 km is the alert, wherever it appears."""
        w = watcher()
        alerts = self._fly_in(w)
        self.assertEqual(len(alerts), 1)
        self.assertLessEqual(alerts[0].distance_km, 10.0)

    def test_a_pass_beyond_the_alert_radius_never_alerts(self):
        """39, 28, 13, 11 km: the last sits inside the exit radius, but never in."""
        w = watcher()
        alerts = self._fly_in(w, offsets=(0.35, 0.25, 0.12, 0.10))
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()

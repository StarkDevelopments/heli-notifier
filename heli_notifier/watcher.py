"""The live alerter: poll, decide, and remember who is already inside.

The state machine is the whole design. Alerting is **entry-triggered**: an
aircraft raises a notification when it crosses *into* the radius, not on every
poll it is inside. This keeps repeated notifications for the same pass from
overwhelming the person receiving them.

Three details keep that from being fragile:

* **Exit hysteresis.** An aircraft is considered still inside until it is past
  ``radius * exit_hysteresis``. An aircraft orbiting on the boundary would
  otherwise flip in and out of state and fire repeatedly.
* **Gap tolerance.** An aircraft is not treated as departed until it has been
  missing for ``gap_polls`` polls. A single dropped poll, which adsb.fi produces
  routinely, must not reset the state and re-alert.
* **Enrichment is optional.** The adsbdb lookup happens after the decision and
  is wrapped so that a failure costs the registration and nothing else.

The poll radius (--radius, ~15 NM by default) only has to cover the alert
radius plus its exit hysteresis. Polling a much wider circle finds helicopters
that are never going to alert, which is a courtesy to the feed rather than to
the user.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .adsbdb import AdsbDbClient, Identity
from .alerts import Alert, build_alert
from .geo import haversine_m, initial_bearing_deg
from .notify import LogNotifier, Notifier
from .sources.adsbfi import AdsbFiError, Aircraft, build_client, iter_unique
from .watchlist import Match, WatchConfig, classify

log = logging.getLogger(__name__)

#: How often the loop prints a stats line. Every poll would be 2,880 lines
#: over a day and would bury the failures that matter; never would make an
#: unattended run indistinguishable from a hung one.
SUMMARY_EVERY_POLLS = 20

#: Where the loop records "I am still turning", for the container healthcheck.
#: Wall-clock seconds, because the healthcheck is a separate process and
#: time.monotonic() is meaningless across processes.
HEARTBEAT_PATH = Path("/tmp/heli-notifier-heartbeat")


@dataclass
class WatchStats:
    polls_attempted: int = 0
    polls_failed: int = 0
    aircraft_seen: int = 0
    matched: int = 0
    ground_skipped: int = 0
    alerts_raised: int = 0
    notify_failures: int = 0

    def summary(self) -> str:
        text = (
            f"{self.polls_attempted} polls ({self.polls_failed} failed), "
            f"{self.aircraft_seen} sightings, {self.matched} matched, "
            f"{self.alerts_raised} alerts"
        )
        # Only when non-zero, so a healthy run stays quiet about it. This is the
        # only line a long-running alerter ever prints on a boring day, and it
        # has to be enough: a revoked token, an exhausted monthly quota or a bad
        # device name makes every send fail while "alerts" keeps climbing, and
        # without this the alerter looks perfectly healthy while notifying nobody.
        if self.notify_failures:
            text += f", {self.notify_failures} NOTIFICATION FAILURES"
        return text


@dataclass
class _Tracked:
    """One aircraft currently believed to be inside the radius."""

    entered_at: datetime
    match: Match
    last_seen_poll: int


@dataclass
class Watcher:
    """Poll adsb.fi and raise an alert when a watched aircraft enters."""

    lat: float
    lon: float
    config: WatchConfig = field(default_factory=WatchConfig)
    notifier: Notifier = field(default_factory=LogNotifier)
    dry_run: bool = False
    #: Re-notify an aircraft that has been inside for this long. Police
    #: helicopters work a scene for hours; one notification is right, but if it
    #: is still there two hours later you may not have seen the first.
    loiter_reminder_seconds: float = 2 * 3600.0
    #: Suppress a second non-emergency alert for the same callsign within this
    #: window. Air ambulance services routinely fly two aircraft in formation on
    #: one job, and two identical notifications for one incident is exactly the
    #: behaviour that gets a phone alert switched off for good.
    duplicate_window_seconds: float = 600.0
    #: Where to record loop liveness for the container healthcheck. ``None``
    #: disables it (the tests drive poll_once directly, not the loop).
    heartbeat_path: Path | None = field(default=HEARTBEAT_PATH)

    def __post_init__(self) -> None:
        self.stats = WatchStats()
        self._tracked: dict[str, _Tracked] = {}
        self._identities: dict[str, Identity | None] = {}
        self._recent_callsigns: dict[str, datetime] = {}
        self._adsbdb: AdsbDbClient | None = None
        self._stop = False
        self._client = None
        self.radius_m = self.config.radius_km * 1000.0
        self.exit_radius_m = self.radius_m * self.config.exit_hysteresis

    # -- wiring --------------------------------------------------------------

    def bind(
        self, client, adsbdb: AdsbDbClient | None = None
    ) -> None:
        """Inject the feed client and the identity lookup (used by tests)."""
        self._client = client
        self._adsbdb = adsbdb

    def _ensure_client(self):
        if self._client is None:
            from .config import Config

            self._client = build_client(Config(lat=self.lat, lon=self.lon))
        return self._client

    def request_stop(self) -> None:
        self._stop = True

    # -- matching ------------------------------------------------------------

    def evaluate(self, aircraft: Aircraft) -> tuple[Match, float, float, bool] | None:
        """Assess one aircraft.

        Returns ``(match, distance_km, bearing_deg, within_entry_radius)`` if
        the aircraft is watched and inside the *exit* radius, else ``None``.

        The two radii are separate on purpose. ``within_entry_radius`` gates
        whether a *new* alert fires; the exit radius only decides how long an
        aircraft keeps its "already inside" status. Collapsing them would either
        delay the alert by the hysteresis margin or re-fire on every boundary
        wobble.
        """
        distance_m = haversine_m(self.lat, self.lon, aircraft.lat, aircraft.lon)
        if distance_m > self.exit_radius_m:
            return None
        bearing = initial_bearing_deg(self.lat, self.lon, aircraft.lat, aircraft.lon)
        match = classify(aircraft.callsign, aircraft.squawk, distance_m / 1000.0, self.config)
        if match is None:
            return None
        return match, distance_m / 1000.0, bearing, distance_m <= self.radius_m

    # -- one poll ------------------------------------------------------------

    def poll_once(self, when: datetime | None = None, aircraft: Iterable[Aircraft] | None = None):
        """Run one poll and return the alerts it raised.

        ``aircraft`` bypasses the network, which is how the tests drive the
        state machine through a scripted sequence of positions.
        """
        when = when or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        self.stats.polls_attempted += 1
        poll_number = self.stats.polls_attempted

        if aircraft is None:
            try:
                result = self._ensure_client().fetch()
            except AdsbFiError as exc:
                self.stats.polls_failed += 1
                log.warning("poll %d failed: %s", poll_number, exc)
                self._expire(poll_number)
                return []
            except Exception:  # noqa: BLE001
                self.stats.polls_failed += 1
                log.exception("unexpected error during poll %d", poll_number)
                self._expire(poll_number)
                return []
            aircraft = iter_unique(result.aircraft)
        else:
            aircraft = list(aircraft)

        self.stats.aircraft_seen += len(aircraft)
        alerts: list[Alert] = []

        for craft in aircraft:
            if craft.on_ground:
                # A helicopter parked on a pad at its base is inside the radius
                # and wears a watched callsign. Alerting on it would be the
                # single loudest way to get this switched off again.
                self.stats.ground_skipped += 1
                continue
            found = self.evaluate(craft)
            if found is None:
                continue
            match, distance_km, bearing, within_entry = found
            self.stats.matched += 1
            tracked = self._tracked.get(craft.icao24)
            if tracked is None:
                # Seen for the first time. Only alert if it is actually within
                # the entry radius: an aircraft appearing for the first time
                # out at 22 km is not a new arrival worth interrupting for.
                if not within_entry:
                    continue
                self._consider(craft, when, match, distance_km, bearing, alerts)
                self._tracked[craft.icao24] = _Tracked(
                    entered_at=when, match=match, last_seen_poll=poll_number
                )
            else:
                tracked.last_seen_poll = poll_number
                # Escalate only when an aircraft already inside becomes an
                # emergency, e.g. it starts squawking 7700 while loitering.
                # Priority also rises as an aircraft closes inside 10 km, and
                # that must NOT re-alert: it is the same arrival getting nearer,
                # and treating it as news doubled every notification.
                if match.is_emergency and not tracked.match.is_emergency:
                    tracked.match = match
                    self._consider(craft, when, match, distance_km, bearing, alerts)
                elif not match.is_emergency and self._reminder_due(tracked, when):
                    tracked.entered_at = when
                    self._consider(craft, when, match, distance_km, bearing, alerts)

        # Aircraft that left the exit radius are *not* removed here. Doing so
        # would make a single dropped poll look like a departure and re-alert on
        # the next sighting. _expire() does it once they have been gone long
        # enough to be believed gone.
        self._expire(poll_number)

        for alert in alerts:
            self.stats.alerts_raised += 1
            if self.dry_run:
                log.info("dry-run alert: %s", alert.title)
                continue
            if not self.notifier.send(alert):
                self.stats.notify_failures += 1
        return alerts

    def _reminder_due(self, tracked: _Tracked, when: datetime) -> bool:
        if self.loiter_reminder_seconds <= 0:
            return False
        return (when - tracked.entered_at).total_seconds() >= self.loiter_reminder_seconds

    def _expire(self, poll_number: int) -> None:
        """Drop tracked aircraft that have been absent for too many polls."""
        if self.config.gap_polls <= 0:
            return
        for icao24, tracked in list(self._tracked.items()):
            if poll_number - tracked.last_seen_poll > self.config.gap_polls:
                del self._tracked[icao24]

    def _identity(self, craft: Aircraft) -> Identity | None:
        """Cached adsbdb lookup, by hex first and then by callsign."""
        if self._adsbdb is None:
            return None
        if craft.icao24 in self._identities:
            return self._identities[craft.icao24]
        identity = self._adsbdb.identity(craft.icao24) or self._adsbdb.identity(
            craft.callsign
        )
        self._identities[craft.icao24] = identity
        return identity

    def _consider(
        self,
        craft: Aircraft,
        when: datetime,
        match: Match,
        distance_km: float,
        bearing: float,
        alerts: list[Alert],
    ) -> None:
        """Raise an alert unless this callsign was already notified moments ago."""
        if self._is_duplicate(craft, when, match):
            return
        alerts.append(self._alert_for(craft, when, match, distance_km, bearing))

    def _is_duplicate(self, craft: Aircraft, when: datetime, match: Match) -> bool:
        """True if this callsign already alerted recently.

        Keyed on the callsign rather than the airframe, so two aircraft sharing
        one callsign collapse into a single notification. Emergency alerts are
        never suppressed: a squawk change is news even if the same callsign was
        notified a minute ago.
        """
        callsign = craft.callsign
        if not callsign or match.is_emergency:
            return False
        previous = self._recent_callsigns.get(callsign)
        if previous is not None and (when - previous).total_seconds() < self.duplicate_window_seconds:
            log.info("suppressed duplicate %s alert (already sent recently)", callsign)
            return True
        self._recent_callsigns[callsign] = when
        self._prune_recent(when)
        return False

    def _prune_recent(self, when: datetime) -> None:
        cutoff = when.timestamp() - max(self.duplicate_window_seconds, 3600.0)
        for callsign, seen in list(self._recent_callsigns.items()):
            if seen.timestamp() < cutoff:
                del self._recent_callsigns[callsign]

    def _alert_for(
        self, craft: Aircraft, when: datetime, match: Match, distance_km: float, bearing: float
    ) -> Alert:
        identity = self._identity(craft)
        alt = craft.baro_alt_ft if craft.baro_alt_ft is not None else craft.geo_alt_ft
        return build_alert(
            match,
            icao24=craft.icao24,
            when=when,
            distance_km=distance_km,
            bearing_deg=bearing,
            baro_alt_ft=alt,
            ground_speed_kt=craft.ground_speed_kt,
            track_deg=craft.track_deg,
            registration=identity.registration if identity else None,
            type_name=identity.type_name if identity else None,
        )

    # -- loop ----------------------------------------------------------------

    def _touch_heartbeat(self, interval: float) -> None:
        """Record that the loop completed an iteration.

        Written after every poll, including failed ones: the loop being alive
        is what the healthcheck measures, not the feed being reachable. The
        interval rides along so the healthcheck can scale its threshold without
        being configured separately. A failed write must not take the alerter
        down, so OSError is swallowed -- the healthcheck then reports
        unhealthy, which is the correct signal for "cannot record liveness".
        """
        if self.heartbeat_path is None:
            return
        try:
            tmp = self.heartbeat_path.parent / (self.heartbeat_path.name + ".tmp")
            tmp.write_text(f"{time.time():.3f} {interval:g}\n")
            # Atomic replace: the healthcheck is a separate process and must
            # never read a half-written stamp, same as the adsbdb cache.
            tmp.replace(self.heartbeat_path)
        except OSError:
            log.warning("could not write heartbeat to %s", self.heartbeat_path)

    def run(self, *, once: bool = False) -> WatchStats:
        """Poll forever, or once if ``once`` is set."""
        interval = max(1, self.config.interval_seconds)
        try:
            while not self._stop:
                started = time.monotonic()
                self.poll_once()
                self._touch_heartbeat(interval)
                if self.stats.polls_attempted % SUMMARY_EVERY_POLLS == 0:
                    log.info("watch: %s", self.stats.summary())
                if once:
                    break
                remaining = interval - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            log.info("watch finished: %s", self.stats.summary())
        return self.stats

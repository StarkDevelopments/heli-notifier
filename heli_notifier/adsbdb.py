"""Aircraft identity lookups, cached to disk and never allowed to break an alert.

adsb.fi carries no operator field, so the only way to turn ``405f81`` into
"G-HEOI, National Police Air Service" is a second lookup. Two things about the
API are worth recording because both are easy to get wrong:

* The documented ``/v0/aircraft/reg/{reg}`` and ``/v0/aircraft/mode_s/{hex}``
  paths both return **404**. The live endpoint is the bare
  ``/v0/aircraft/{identifier}``, which accepts either a registration or a hex.
* ``registered_owner`` is the *operating company*, not the service. Nine of the
  local air ambulance registrations resolve to "Babcock MCS Onshore", so that
  field is deliberately not even stored here: reading it as the operator would
  be wrong. Service names come from the label table instead.

Everything here is best-effort. A lookup failure must never delay or suppress a
notification, so every failure path returns ``None`` and the alert is sent with
whatever the adsb.fi feed already gave us.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

ADSBDB_BASE_URL = "https://api.adsbdb.com/v0/aircraft"

#: adsbdb asks for no more than one request per second.
MIN_INTERVAL_SECONDS = 1.1

#: Registrations and operator names do not change, but a caller's local
#: knowledge of them does. A week is short enough to pick up a re-registration
#: and long enough to stay well clear of the rate limit.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

DEFAULT_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class Identity:
    """What an aircraft is, beyond the fields the feed provides."""

    registration: str | None = None
    type_name: str | None = None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


class AdsbDbClient:
    """Cached, rate-limited adsbdb lookups keyed by hex or registration."""

    def __init__(
        self,
        cache_path: Path,
        *,
        base_url: str = ADSBDB_BASE_URL,
        user_agent: str = "heli-notifier/0.1 (personal aircraft alerter; contact: local)",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        fetch: Callable[[str], dict[str, Any] | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_path = cache_path
        self.base_url = base_url
        self.user_agent = user_agent
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self._fetch_impl = fetch or self._http_fetch
        #: Monotonic time is only valid *within* one process, so it paces the
        #: rate limiter and nothing else. Cache expiry is stamped with the wall
        #: clock, because the cache outlives the process that wrote it: a
        #: persisted monotonic value is meaningless after a restart and would
        #: read as permanently fresh.
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._last_request = 0.0
        self._cache: dict[str, dict[str, Any]] = {}
        self._loaded = False
        #: Counters so the log can show whether lookups are actually working.
        self.hits = 0
        self.misses = 0
        self.failures = 0

    # -- cache ---------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self.cache_path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError:
            log.warning("could not read adsbdb cache %s", self.cache_path)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("adsbdb cache %s is not valid JSON; ignoring", self.cache_path)
            return
        if isinstance(data, dict):
            entries = data.get("entries")
            if isinstance(entries, dict):
                self._cache = {
                    str(k): v for k, v in entries.items() if isinstance(v, dict)
                }

    def _save(self) -> None:
        payload = {
            "version": 1,
            "entries": self._cache,
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            # Write via a temporary file: the watcher is long-running and a
            # half-written cache would be worse than a missing one.
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            log.warning("could not write adsbdb cache %s", self.cache_path)

    def _entry_fresh(self, entry: dict[str, Any]) -> bool:
        try:
            stored = float(entry["stored_at"])
        except (KeyError, TypeError, ValueError):
            return False
        age = self._wall_clock() - stored
        # A negative age means the stamp is in the future, which a clock change
        # or a hand-edited cache can produce. Treat it as stale rather than
        # trusting it indefinitely.
        return 0 <= age < self.ttl_seconds

    # -- lookup --------------------------------------------------------------

    def identity(self, identifier: str | None) -> Identity | None:
        """Look up one aircraft, or return ``None`` if unknown or unavailable."""
        if not identifier:
            return None
        key = identifier.strip().lower()
        if not key:
            return None
        self._load()

        entry = self._cache.get(key)
        if entry is not None and self._entry_fresh(entry):
            self.hits += 1
            return self._to_identity(entry.get("aircraft") or {})

        # A stale entry is better than no entry when the network is down, and
        # registrations rarely change, so refresh opportunistically but serve
        # the old answer if the refresh fails.
        aircraft = self._lookup(key)
        if aircraft is None:
            self.failures += 1
            if entry is not None:
                return self._to_identity(entry.get("aircraft") or {})
            return None

        self.misses += 1
        self._cache[key] = {
            "stored_at": self._wall_clock(),
            "aircraft": aircraft,
        }
        self._save()
        return self._to_identity(aircraft)

    def _lookup(self, key: str) -> dict[str, Any] | None:
        wait = MIN_INTERVAL_SECONDS - (self._monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._monotonic()
        try:
            return self._fetch_impl(key)
        except Exception:  # noqa: BLE001 - a lookup must never break an alert
            log.exception("adsbdb lookup failed for %s", key)
            return None

    def _http_fetch(self, key: str) -> dict[str, Any] | None:
        url = f"{self.base_url.rstrip('/')}/{key}"
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            log.warning("adsbdb returned HTTP %s for %s", exc.code, key)
            return None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        aircraft = payload.get("response", {})
        if not isinstance(aircraft, dict):
            return None
        aircraft = aircraft.get("aircraft")
        return aircraft if isinstance(aircraft, dict) else None

    @staticmethod
    def _to_identity(aircraft: dict[str, Any]) -> Identity:
        return Identity(
            registration=_clean(aircraft.get("registration")),
            type_name=_clean(aircraft.get("type")),
        )

"""Client for the adsb.fi v3 open-data API.

The endpoint is a radius query, so the distance filter is applied server-side
and no bounding box is needed:

    GET /api/v3/lat/{lat}/lon/{lon}/dist/{nm}

Documented behaviour that this module has to accommodate:

* `alt_baro` is sometimes the *string* `"ground"` rather than a number. Passing
  that to int() raises ValueError and would abort a poll, so every numeric
  field goes through a coercion helper.
* `alt_geom`, `track`, and `baro_rate` are absent on some aircraft. Nothing may
  assume they exist.
* `mlat` is usually an empty list, meaning the position is raw ADS-B with no
  multilateration applied.
* Aircraft may carry no position at all, in which case `lat`/`lon` are null and
  the aircraft is skipped.

Only the standard library is used, so the alerter runs on a bare Python install
with no build step.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

ADSBFI_BASE_URL = "https://opendata.adsb.fi/api"

#: adsb.fi asks that consumers be identifiable. A descriptive User-Agent is
#: enough; no browser spoofing is required on the v3 endpoint.
DEFAULT_USER_AGENT = "heli-notifier/0.1 (personal aircraft alerter)"

#: Documented public limit is 1 request per second. The alerter polls far more
#: slowly than this, so the floor only guards against a misconfiguration.
MIN_INTERVAL_SECONDS = 1.0


class AdsbFiError(RuntimeError):
    """Raised when a poll cannot be completed."""


def coerce_alt(value: Any) -> int | None:
    """Coerce an adsb.fi altitude field to feet, or None.

    Handles the `"ground"` sentinel, nulls, and numeric strings. Booleans are
    rejected explicitly because bool is a subclass of int in Python and would
    otherwise silently become 0 or 1 feet.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.lower() in {"ground", "n/a", "none", "null", "-"}:
            return None
        try:
            return int(float(stripped))
        except ValueError:
            return None
    return None


def coerce_float(value: Any) -> float | None:
    """Coerce a field to float, or None. Used for speed, track, and rate."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def normalise_callsign(value: Any) -> str | None:
    """Trim the callsign. The feed pads it and sends null for unidentified aircraft."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True)
class Aircraft:
    """One aircraft as reported by adsb.fi, with the raw values preserved."""

    icao24: str
    lat: float
    lon: float
    callsign: str | None = None
    baro_alt_ft: int | None = None
    geo_alt_ft: int | None = None
    ground_speed_kt: float | None = None
    track_deg: float | None = None
    vertical_rate: float | None = None
    squawk: str | None = None
    category: str | None = None
    type_code: str | None = None
    description: str | None = None
    emergency: str | None = None
    is_mlat: bool = False
    rssi_dbm: float | None = None
    seen_pos: float | None = None
    seen: float | None = None
    baro_rate: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def on_ground(self) -> bool:
        """True when the aircraft is reported on the ground.

        The feed uses `alt_baro: "ground"`, which coerces to None, so a None
        barometric altitude is treated as ground-level rather than unknown.
        An aircraft with a genuine barometric altitude is treated as airborne
        regardless of ground speed, because a 5 kt aircraft taxiing on a
        runway still has an altitude reading.
        """
        if self.baro_alt_ft is None:
            return True
        if self.baro_alt_ft == 0:
            return True
        return False


@dataclass
class PollResult:
    """The outcome of a single poll."""

    fetched_at: datetime
    aircraft: list[Aircraft]
    total: int | None = None
    message: str | None = None
    elapsed_seconds: float = 0.0


def _parse_aircraft(entry: dict[str, Any]) -> Aircraft | None:
    """Convert one raw entry, or None if it has no usable position."""
    lat = coerce_float(entry.get("lat"))
    lon = coerce_float(entry.get("lon"))
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None

    icao = entry.get("hex") or entry.get("icao24")
    if not icao:
        return None

    mlat = entry.get("mlat")
    return Aircraft(
        icao24=str(icao).strip().lower(),
        lat=lat,
        lon=lon,
        callsign=normalise_callsign(entry.get("flight")),
        baro_alt_ft=coerce_alt(entry.get("alt_baro")),
        geo_alt_ft=coerce_alt(entry.get("alt_geom")),
        ground_speed_kt=coerce_float(entry.get("gs")),
        track_deg=coerce_float(entry.get("track")),
        vertical_rate=coerce_float(entry.get("baro_rate")),
        squawk=(str(entry["squawk"]).strip() or None) if entry.get("squawk") else None,
        category=normalise_callsign(entry.get("category")),
        type_code=normalise_callsign(entry.get("t")),
        description=normalise_callsign(entry.get("desc")),
        emergency=normalise_callsign(entry.get("emergency")),
        is_mlat=bool(mlat),
        rssi_dbm=coerce_float(entry.get("rssi")),
        seen_pos=coerce_float(entry.get("seen_pos")),
        seen=coerce_float(entry.get("seen")),
        baro_rate=coerce_float(entry.get("baro_rate")),
        raw=entry,
    )


class AdsbFiClient:
    """Polls the adsb.fi v3 endpoint.

    Retries transient failures with exponential backoff, but does not retry on
    a client error such as a 404, which would only repeat.
    """

    def __init__(
        self,
        url: str,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        min_interval: float = MIN_INTERVAL_SECONDS,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.min_interval = min_interval
        self._last_request_monotonic: float | None = None

    def _respect_rate_limit(self) -> None:
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    def fetch(self) -> PollResult:
        """Perform one poll, returning the parsed aircraft.

        Raises AdsbFiError if every attempt fails.
        """
        started = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._respect_rate_limit()
            try:
                result = self._fetch_once(started)
                self._last_request_monotonic = time.monotonic()
                return result
            except urllib.error.HTTPError as exc:
                last_error = exc
                # 4xx other than 429 will not fix themselves; stop early.
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_error = exc
            except ValueError as exc:
                # Malformed payload; a retry is reasonable, but bounded.
                last_error = exc

            if attempt < self.max_retries:
                time.sleep(self.backoff_base**attempt)

        raise AdsbFiError(f"poll failed after {self.max_retries + 1} attempts: {last_error}") from last_error

    def _fetch_once(self, started: float) -> PollResult:
        request = urllib.request.Request(
            self.url,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise AdsbFiError(f"unexpected HTTP status {response.status}")
            body = response.read()

        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise AdsbFiError("payload was not a JSON object")

        entries = payload.get("ac")
        if entries is None:
            raise AdsbFiError("payload had no 'ac' array")
        if not isinstance(entries, list):
            raise AdsbFiError("'ac' was not an array")

        aircraft: list[Aircraft] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            parsed = _parse_aircraft(entry)
            if parsed is not None:
                aircraft.append(parsed)

        return PollResult(
            fetched_at=datetime.now(timezone.utc),
            aircraft=aircraft,
            total=coerce_float(payload.get("total")),
            message=payload.get("msg") if isinstance(payload.get("msg"), str) else None,
            elapsed_seconds=time.monotonic() - started,
        )


def build_client(config, base_url: str = ADSBFI_BASE_URL, **kwargs) -> AdsbFiClient:
    """Construct a client pointed at this config's location."""
    return AdsbFiClient(config.url_for(base_url), user_agent=config.user_agent, **kwargs)


def iter_unique(aircraft: Iterable[Aircraft]) -> list[Aircraft]:
    """Collapse duplicate ICAO24 entries, keeping the first occurrence.

    The feed occasionally lists the same aircraft twice during a merge.
    """
    seen: set[str] = set()
    out: list[Aircraft] = []
    for item in aircraft:
        if item.icao24 in seen:
            continue
        seen.add(item.icao24)
        out.append(item)
    return out

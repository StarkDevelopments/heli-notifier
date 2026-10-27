"""Where we are, and how wide we poll.

The alerter's own behaviour -- what counts as watched, at what radius it fires
-- lives in :mod:`heli_notifier.watchlist`. This module only supplies the feed
query: the centre point, the poll radius, and the directory holding the
identity-lookup cache.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: The watched position has no default on purpose: it is site data, so it
#: comes from .env (HELI_NOTIFIER_LAT / HELI_NOTIFIER_LON) or a flag and never
#: appears in this file, the repo, or the image. A missing position fails
#: validation by name rather than quietly watching empty sky.

#: Poll radius in nautical miles. It must comfortably exceed the alert radius
#: plus its exit hysteresis (10 km * 1.15 = 11.5 km = 6.2 NM), otherwise an
#: aircraft on the hysteresis band drops out of the feed and the alerter
#: forgets it while it is still overhead. 15 NM is ~28 km: ample margin.
#: Widen it with --radius if you raise --alert-radius.
DEFAULT_RADIUS_NM = 15.0

#: adsb.fi caps the radius parameter at 250 NM.
MAX_RADIUS_NM = 250.0


@dataclass(frozen=True)
class Config:
    lat: float | None = None
    lon: float | None = None
    radius_nm: float = DEFAULT_RADIUS_NM
    #: Only used by the identity-lookup cache; nothing else is written here.
    data_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "data")
    user_agent: str = "heli-notifier/0.1 (personal aircraft alerter; contact: local)"

    @property
    def adsbdb_cache_path(self) -> Path:
        return self.data_dir / "adsbdb-cache.json"

    def url_for(self, base_url: str) -> str:
        """The adsb.fi v3 radius query for this location."""
        return (
            f"{base_url.rstrip('/')}/v3/lat/{self.lat:.5f}"
            f"/lon/{self.lon:.5f}/dist/{self.radius_nm:g}"
        )

    def validate(self) -> list[str]:
        """Return a list of problems. Empty means valid."""
        problems: list[str] = []
        if self.lat is None:
            problems.append("latitude not set (set HELI_NOTIFIER_LAT or --lat)")
        elif not -90.0 <= self.lat <= 90.0:
            problems.append(f"latitude {self.lat} out of range")
        if self.lon is None:
            problems.append("longitude not set (set HELI_NOTIFIER_LON or --lon)")
        elif not -180.0 <= self.lon <= 180.0:
            problems.append(f"longitude {self.lon} out of range")
        if not 0 < self.radius_nm <= MAX_RADIUS_NM:
            problems.append(f"radius {self.radius_nm} NM outside 0-{MAX_RADIUS_NM}")
        return problems

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Build a config from environment variables, with explicit overrides.

        Environment variables are the interface a container or a systemd unit
        will use; the keyword overrides let the CLI win over the environment.
        """

        def _num(name: str, cast, default):
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            try:
                return cast(raw)
            except ValueError:
                raise ValueError(f"environment variable {name}={raw!r} is not a valid number") from None

        data_dir = os.environ.get("HELI_NOTIFIER_DATA_DIR")
        values = {
            "lat": _num("HELI_NOTIFIER_LAT", float, None),
            "lon": _num("HELI_NOTIFIER_LON", float, None),
            "radius_nm": _num("HELI_NOTIFIER_RADIUS_NM", float, DEFAULT_RADIUS_NM),
            "data_dir": Path(data_dir) if data_dir else Path(__file__).resolve().parent.parent / "data",
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

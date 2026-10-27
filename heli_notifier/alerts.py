"""Turning one matched aircraft into something worth reading on a phone.

A phone alert has one job: tell the reader *where to look*. Three things carry
that:

* a **16-wind compass point**, not a degree bearing. 215 degrees is precise and
  useless to someone deciding which way to turn in the dark.
* **time to overhead**, estimated from the closing component of the ground
  speed. A helicopter 15 km out that is 4 minutes from overhead is actionable;
  the same aircraft 40 seconds from overhead is a surprise, not a warning.
* the **operator name**, so "HLE22" resolves to "Wiltshire and Bath Air
  Ambulance" and the alert is worth reading rather than swiping away.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .geo import compass_point
from .watchlist import Match, describe_squawk, squawk_is_the_reason

#: Below this a helicopter is loitering on a scene rather than transiting.
ON_SCENE_SPEED_KT = 70.0

#: Close enough to be worth saying "look up" rather than just a bearing.
OVERHEAD_RADIUS_KM = 5.0


@dataclass(frozen=True)
class Alert:
    """One notification, fully rendered and ready to send."""

    icao24: str
    title: str
    message: str
    priority: int
    when: datetime
    service: str
    callsign: str | None = None
    registration: str | None = None
    type_name: str | None = None
    distance_km: float = 0.0
    bearing_deg: float = 0.0
    closing_kt: float | None = None
    minutes_to_overhead: float | None = None
    reason: str = ""
    squawk: str | None = None

    @property
    def is_emergency(self) -> bool:
        return self.priority >= 2


def closing_speed_kt(
    ground_speed_kt: float | None, track_deg: float | None, bearing_deg: float | None
) -> float | None:
    """Component of ground speed directed at the observer.

    The angle between where the aircraft is pointing and the bearing from it to
    us is 0 when it is flying straight at us, so the cosine of that angle
    projects its speed onto our line of sight. Positive means closing. Returns
    ``None`` when the feed did not supply enough to compute it, because
    guessing 0 would silently read as "stationary".
    """
    if ground_speed_kt is None or track_deg is None or bearing_deg is None:
        return None
    delta = abs(((track_deg - bearing_deg + 180.0) % 360.0) - 180.0)
    return ground_speed_kt * math.cos(math.radians(delta))


def minutes_to_reach(
    distance_km: float, closing_kt: float | None, max_kmh: float = 1.852
) -> float | None:
    """Minutes until the aircraft reaches the observer, if it is closing.

    ``max_kmh`` is knots -> km/h. Capped by returning ``None`` for a receding
    aircraft rather than a negative number that reads like a bug.
    """
    if closing_kt is None or closing_kt <= 1e-6:
        return None
    return distance_km / (closing_kt * max_kmh) * 60.0


def _fmt_alt(alt_ft: int | None) -> str:
    return f"{alt_ft:,} ft" if alt_ft is not None else "altitude unknown"


def _fmt_eta(minutes: float | None) -> str:
    if minutes is None:
        return ""
    if minutes < 1.0:
        return "closing now"
    if minutes < 90:
        return f"{minutes:.0f} min to overhead"
    return ""


def build_alert(
    match: Match,
    *,
    icao24: str,
    when: datetime,
    distance_km: float,
    bearing_deg: float,
    baro_alt_ft: int | None = None,
    ground_speed_kt: float | None = None,
    track_deg: float | None = None,
    registration: str | None = None,
    type_name: str | None = None,
) -> Alert:
    """Render one matched aircraft into a notification."""
    when = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
    closing = closing_speed_kt(ground_speed_kt, track_deg, bearing_deg)
    eta = minutes_to_reach(distance_km, closing)
    wind = compass_point(bearing_deg)
    callsign = match.callsign or (registration or icao24)
    service = match.service

    # Title carries the two facts you act on: what it is and where it is.
    title = f"{callsign} - {distance_km:.1f} km {wind}"
    if match.is_emergency:
        title = f"EMERGENCY - {title}"

    lines: list[str] = [service]
    detail = _fmt_alt(baro_alt_ft)
    if ground_speed_kt is not None:
        detail += f", {ground_speed_kt:.0f} kt"
    if closing is not None:
        detail += f", {closing:+.0f} kt {'closing' if closing > 0 else 'receding'}"
    lines.append(detail)

    tail = _fmt_eta(eta)
    if ground_speed_kt is not None and ground_speed_kt <= ON_SCENE_SPEED_KT:
        tail = f"likely on scene{', ' + tail if tail else ''}"
    if tail:
        lines.append(tail)

    extras = []
    if registration:
        extras.append(registration)
    if type_name:
        extras.append(type_name)
    if extras:
        lines.append(" - ".join(extras))
    if match.squawk and not squawk_is_the_reason(match.reason):
        lines.append(f"squawk {match.squawk} ({describe_squawk(match.squawk)})")
    if distance_km <= OVERHEAD_RADIUS_KM and not tail:
        # "Overhead" is a claim about a few hundred metres. At the 5 km close
        # mark an aircraft is still plainly visible in the sky, so say so
        # rather than implying it is directly above the observer.
        lines.append(f"within {distance_km:.1f} km, look up")

    return Alert(
        icao24=icao24,
        title=title,
        message="\n".join(lines),
        priority=match.priority,
        when=when,
        service=service,
        callsign=match.callsign,
        registration=registration,
        type_name=type_name,
        distance_km=distance_km,
        bearing_deg=bearing_deg,
        closing_kt=closing,
        minutes_to_overhead=eta,
        reason=match.reason,
        squawk=match.squawk,
    )

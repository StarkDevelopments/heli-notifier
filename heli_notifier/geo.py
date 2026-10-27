"""Geometry helpers for the alerter.

Distances use the haversine formula on a spherical earth. That is accurate to
roughly 0.3% against the WGS84 ellipsoid, which is far below the uncertainty in
ADS-B position itself, so it is not worth carrying an ellipsoidal model here.

The unit convention throughout this module is explicit at every boundary:
    * distances passed in and returned from public helpers are METRES
    * angles are DEGREES unless the name says otherwise
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6371008.8

#: The 16-wind compass rose. A degree bearing is precise and useless to someone
#: standing outside in the dark deciding which way to turn, so alerts report the
#: wind name instead.
COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def compass_point(bearing_deg: float) -> str:
    """Name the 16-wind sector a bearing falls in.

    Sectors are centred on their name, so 0 is dead north rather than the
    north-north-east sector, and each spans +/-11.25 degrees.
    """
    return COMPASS_POINTS[int((bearing_deg % 360.0) / 22.5 + 0.5) % 16]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 coordinates."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing in degrees, normalised to [0, 360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return math.degrees(math.atan2(y, x)) % 360.0

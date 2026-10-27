"""Which aircraft are worth waking someone up for.

The design question here is *what to match on*. Classifying by aircraft type
would require interpreting the free-text ``description`` field, which can be
inconsistent. Operator designators and emergency squawks provide more reliable
signals.

Matching on the ICAO operator designator instead is both cheaper and more
reliable, because those designators are assigned by the CAA rather than inferred:

* ``HLE`` is the ICAO three-letter operator designator for **all** UK HEMS
  (helicopter emergency medical service) traffic. It is not a per-airline code:
  Great Western, Devon, Wales and Dorset & Somerset all fly under it.
* ``HM`` is "Helimed", the UK HEMS radiotelephony callsign. Wales Air Ambulance
  has had airframes broadcast under *both* ``HLE`` and ``HM`` (``G-WYDN`` as
  HLE67, ``G-WENU`` as HM67), so matching only ``HLE`` misses aircraft.
* ``UKP`` is the police designator used by the National Police Air Service.

Per the CAA callsign policy, HEMS flights squawk ``0020`` unless promulgated
otherwise, which gives a second, independent signal. Squawks ``7700``/``7500``/
``7600`` (general emergency, hijack, radio failure) are matched regardless of
operator: those are worth an alert from *any* aircraft, not just a
pre-selected list, and they are exactly the case a watchlist would miss.

The one thing this cannot do is name the operator. adsb.fi carries no operator
field, and adsbdb's ``registered_owner`` is the *operating company* -- six of the
nine air ambulance registrations resolve to "Babcock MCS Onshore" -- so the
service names live in a local label table instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

#: ICAO operator designators for UK HEMS. ``HLE`` is the allocated designator;
#: ``HM`` covers airframes broadcasting the "Helimed" radiotelephony callsign.
HEMS_PREFIXES: tuple[str, ...] = ("HLE", "HM")

#: Police. The CAA requires "POLICE [n] Zulu"; feeds carry the UKP designator.
POLICE_PREFIXES: tuple[str, ...] = ("UKP",)

#: Squawk assigned to UK air ambulance units (CAA: HEMS flights shall select
#: 0020 or another code promulgated in AIP ENR 1.6).
HEMS_SQUAWKS: frozenset[str] = frozenset({"0020"})

#: UK police helicopters carry 006x on the transponder. Watching it catches a
#: police aircraft before its callsign has been decoded out of the signal, which
#: is exactly the moment an alert is most useful.
POLICE_SQUAWKS: frozenset[str] = frozenset({"0061", "0062", "0063", "0064"})

#: Distress/emergency transponder codes. These match any aircraft, whatever its
#: operator, because a squawk of 7700 is the aircraft telling the world it has
#: an emergency.
EMERGENCY_SQUAWKS: frozenset[str] = frozenset({"7700", "7500", "7600"})

#: Fallback names, used when a callsign is not in the local label table. These
#: are deliberately generic: guessing a specific trust from a two-digit code is
#: how a label table starts lying to you.
DEFAULT_SERVICE_LABELS: dict[str, str] = {
    "HLE": "Air ambulance (HEMS)",
    "HM": "Air ambulance (HEMS)",
    "UKP": "National Police Air Service",
}

#: Known local operators, keyed by exact callsign. Overridable with
#: ``--labels`` because airframes rotate between callsigns and this table will
#: go stale; the matching rule must not depend on it.
#:
#: Sourced from the official ICAO UK HEMS designator allocation, then
#: cross-checked against adsbdb: HLE22 (Wiltshire Air
#: Ambulance Charitable Trust), HLE24, HLE70 and HLE71 all confirmed their
#: trust, and UKP01/09/32/152 all came back as National Police Air Service,
#: which is why the police fallback is now specific. HLE10 has no adsbdb record
#: and so rests on the allocation list alone.
#:
#: adsbdb's `registered_owner` is deliberately not trusted here: it names the
#: airframe's owner, which for several of these is Babcock MCS Onshore, and
#: reading that as the operator would be wrong.
LOCAL_SERVICE_LABELS: dict[str, str] = {
    "HLE65": "Great Western Air Ambulance",
    "HLE22": "Wiltshire and Bath Air Ambulance",
    "HLE10": "Dorset and Somerset Air Ambulance",
    "HLE70": "Devon Air Ambulance",
    "HLE71": "Devon Air Ambulance",
    "HLE24": "Thames Valley Air Ambulance",
    "HLE80": "The Air Ambulance Service",
    "HLE59": "Wales Air Ambulance",
    "HLE67": "Wales Air Ambulance",
    "HM67": "Wales Air Ambulance",
    "HM59": "Wales Air Ambulance",
}

#: How an aircraft qualified. Kept on the alert so a notification can explain
#: *why* it fired, which is what makes an unexpected alert diagnosable.
MATCH_HEMS = "hems-callsign"
MATCH_POLICE = "police-callsign"
MATCH_HEMS_SQUAWK = "hems-squawk"
MATCH_POLICE_SQUAWK = "police-squawk"
MATCH_EMERGENCY_SQUAWK = "emergency-squawk"

#: Priority tiers. Pushover uses 0 (normal) / 1 (high) / 2 (emergency).
PRIORITY_NORMAL = 0
PRIORITY_HIGH = 1
PRIORITY_EMERGENCY = 2

#: Only a squawk of distress escalates to 2. An air ambulance 3 km away is
#: genuinely interesting, but it is a routine landing and must not be allowed
#: to bypass someone's Do Not Disturb at 02:00 -- that is what priority 2 does,
#: and the whole point of this alerter is to be *wanted*, not to be muted.
#: Half the alert radius, so "close" and "just arrived" stay distinct bands.
PRIORITY_HIGH_RADIUS_KM = 5.0


@dataclass(frozen=True)
class WatchConfig:
    """Everything the alerter needs that the feed query does not supply."""

    #: The alert radius is deliberately small: only an aircraft close enough to
    #: see or hear is worth waking for, and anything wider alerts on routine
    #: activity at bases just outside it. A helicopter 100 km away is on the
    #: horizon and about to leave anyway.
    radius_km: float = 10.0
    interval_seconds: int = 30
    hems_prefixes: tuple[str, ...] = HEMS_PREFIXES
    police_prefixes: tuple[str, ...] = POLICE_PREFIXES
    hems_squawks: frozenset[str] = HEMS_SQUAWKS
    police_squawks: frozenset[str] = POLICE_SQUAWKS
    emergency_squawks: frozenset[str] = EMERGENCY_SQUAWKS
    #: An aircraft is only considered departed once it has been missing for this
    #: many polls. Without it, a single dropped poll re-triggers an alert.
    gap_polls: int = 5
    #: Stay "inside" until this multiple of the radius, so an aircraft sitting
    #: on the boundary does not flap in and out of the alert state.
    exit_hysteresis: float = 1.15
    service_labels: dict[str, str] = field(default_factory=lambda: dict(LOCAL_SERVICE_LABELS))

    def label_for(self, callsign: str | None) -> str:
        """Name the operator behind a callsign, falling back to its prefix."""
        if callsign:
            exact = self.service_labels.get(callsign)
            if exact:
                return exact
            for prefix in self.hems_prefixes:
                if callsign.startswith(prefix):
                    return DEFAULT_SERVICE_LABELS.get(prefix, "Air ambulance (HEMS)")
            for prefix in self.police_prefixes:
                if callsign.startswith(prefix):
                    return DEFAULT_SERVICE_LABELS.get(prefix, "Police helicopter")
        return "Aircraft"

    def with_label_file(self, path: Path) -> "WatchConfig":
        """Return a copy with a JSON label file merged over the built-in table.

        Returns a new config rather than mutating: ``LOCAL_SERVICE_LABELS`` is
        module state, and a long-running process that quietly rewrote it would
        make the effective watchlist depend on call order.
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a JSON object of callsign -> name")
        bad = [k for k, v in data.items() if not isinstance(k, str) or not isinstance(v, str)]
        if bad:
            raise ValueError(f"{path} has non-string entries: {sorted(bad)[:3]}")
        merged = dict(self.service_labels)
        merged.update(data)
        return replace(self, service_labels=merged)


@dataclass(frozen=True)
class Match:
    """Why an aircraft qualified for an alert."""

    reason: str
    service: str
    priority: int
    callsign: str | None
    squawk: str | None

    @property
    def is_emergency(self) -> bool:
        return self.priority == PRIORITY_EMERGENCY


def _normalise_squawk(value: str | None) -> str | None:
    """adsb.fi sends squawk as a string, sometimes zero-padded, sometimes not."""
    if not value:
        return None
    digits = value.strip()
    if not digits.isdigit():
        return None
    return digits.zfill(4)[-4:]


def classify(
    callsign: str | None,
    squawk: str | None,
    distance_km: float,
    config: WatchConfig | None = None,
) -> Match | None:
    """Decide whether one aircraft should raise an alert, and at what priority.

    Returns ``None`` when the aircraft is not interesting. Priority is
    distance-based except for a squawk of distress, which escalates immediately
    because it is the only signal here that means something is actually wrong.
    """
    cfg = config or WatchConfig()
    cs = (callsign or "").strip() or None
    sq = _normalise_squawk(squawk)
    callsign_key = cs or ""
    service = cfg.label_for(cs)

    priority = (
        PRIORITY_HIGH if distance_km <= PRIORITY_HIGH_RADIUS_KM else PRIORITY_NORMAL
    )

    if sq and sq in cfg.emergency_squawks:
        # Distress outranks everything, including the operator name. A squawk of
        # distress from an aircraft we cannot otherwise identify still means
        # somebody is in trouble, so name the situation rather than "Aircraft".
        return Match(
            MATCH_EMERGENCY_SQUAWK,
            f"Distress squawk {sq} ({describe_squawk(sq)})",
            PRIORITY_EMERGENCY,
            cs,
            sq,
        )

    for prefix in cfg.hems_prefixes:
        if callsign_key.startswith(prefix):
            return Match(MATCH_HEMS, service, priority, cs, sq)

    for prefix in cfg.police_prefixes:
        if callsign_key.startswith(prefix):
            return Match(MATCH_POLICE, service, priority, cs, sq)

    if sq and sq in cfg.hems_squawks:
        # Caught without a usable callsign. The HEMS designator is missing, so
        # this is a weaker signal and the generic name is the honest one.
        return Match(MATCH_HEMS_SQUAWK, "Air ambulance (HEMS)", priority, cs, sq)

    if sq and sq in cfg.police_squawks:
        # Same reasoning as the HEMS squawk: the police transponder code is
        # real evidence, but with no decoded callsign we do not claim to know
        # which force it belongs to.
        return Match(
            MATCH_POLICE_SQUAWK,
            DEFAULT_SERVICE_LABELS["UKP"],
            priority,
            cs,
            sq,
        )

    return None


def squawk_is_the_reason(reason: str) -> bool:
    """True when the squawk, not the callsign, is what matched the aircraft.

    The alert body only needs to print a squawk that *adds* to what the callsign
    already said. When the squawk was the reason we are already in the business
    of reporting it, and repeating it wastes the reader's one glance.
    """
    return reason in (MATCH_HEMS_SQUAWK, MATCH_POLICE_SQUAWK, MATCH_EMERGENCY_SQUAWK)


def describe_squawk(squawk: str) -> str:
    """Explain a transponder code in its own right.

    Deliberately separate from *why* the aircraft matched, because the two
    disagree in practice: an HLE air ambulance squawking 0061 matches on its
    callsign -- HEMS -- while the code printed next to it is a police code.
    Reading that as written tells the reader the wrong thing.
    """
    return {
        **{s: "HEMS" for s in HEMS_SQUAWKS},
        **{s: "police" for s in POLICE_SQUAWKS},
        "7700": "general emergency",
        "7500": "hijack",
        "7600": "radio failure",
    }.get(squawk, "unassigned")

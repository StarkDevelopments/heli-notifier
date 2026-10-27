"""Delivery. Pushover, and a log-only notifier for testing the logic safely.

There is a deliberate asymmetry here. ``LogNotifier`` is the safe default for
local runs and tests. ``PushoverNotifier`` is opt-in and requires credentials to
be present, so a misconfigured deployment fails at startup rather than silently
logging alerts nobody sees.

Delivery failures are logged and counted but never raised into the poll loop: a
Pushover outage must not stop the alerter or lose the state machine, because the
next entry is still worth alerting on.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from .alerts import Alert

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"

#: Substrings that mean "this is still the template". An unfilled .env is the
#: most likely way credentials get into this process, and accepting the
#: placeholder produces a notifier that looks configured and silently fails to
#: deliver every alert. The safe reading of an unfilled .env is log-only.
PLACEHOLDER_MARKERS = ("replace-with", "your-app-token", "your-user-key", "changeme")

#: 12 hours, sent as `ttl` on every alert. It means "delete this from the phone
#: after 12 hours": an alert that old is stale, and leaving it in the shade
#: invites acting on old news. The alerter no longer uses Pushover's emergency
#: mode, so this is the only thing governing how long a notification lives.
DEFAULT_TTL_SECONDS = 43200

#: Wire priority for every alert. 0 is Pushover's normal priority.
#:
#: The tradeoff is quiet hours: Pushover silently downgrades a normal-priority
#: notification to no sound and no alert during the recipient's configured
#: quiet hours. If that swallows a night-time arrival, the fix is to widen or
#: disable quiet hours in the Pushover app, which is a per-user setting, rather
#: than to raise this -- a higher priority is a real behavioural change to every
#: alert, whereas quiet hours are the user's own declared preference.
#:
#: Priority 2 is never used: it re-notifies until acknowledged, and an air
#: ambulance on scene is not a fire.
WIRE_PRIORITY = 0

#: Sound for every alert. "siren" is one of Pushover's built-ins and is the
#: point of the device: this alerter exists to be noticed over noise and while
#: asleep. Override with PUSHOVER_SOUND, or set it to "none" for silent.
DEFAULT_SOUND = "siren"

#: Pushover rejects bodies larger than this on some plans and images over
#: 10 MB. Not currently used -- this alerter sends text only -- but kept next to
#: the sender so the limit is documented where the request is built.
MAX_MESSAGE_CHARS = 1000


class Notifier(Protocol):
    """Anything that can be handed an :class:`~heli_notifier.alerts.Alert`."""

    def send(self, alert: Alert) -> bool: ...

    def close(self) -> None: ...


class LogNotifier:
    """Write alerts to the log. The default, and the safe default."""

    name = "log"

    def __init__(self) -> None:
        self.sent = 0
        #: Present so every Notifier exposes the same counters, and the caller
        #: can report failures without knowing which one it has.
        self.failed = 0

    def send(self, alert: Alert) -> bool:
        self.sent += 1
        stamp = alert.when.strftime("%H:%M:%S")
        log.info(
            "ALERT %s p%d | %s | %s",
            stamp,
            alert.priority,
            alert.title,
            alert.message.replace("\n", " / "),
        )
        return True

    def close(self) -> None:
        return None


class PushoverNotifier:
    """Send alerts to Pushover, one uniform notification per alert.

    Pushover can be asked to treat a message as an emergency (``priority=2``),
    which repeats it with sound until someone acknowledges it. This alerter
    does not use that, for any alert: an aircraft on scene is not a fire, and
    re-sounding a siren for three hours is how a notification gets muted for
    good. Every alert is sent at normal priority instead -- see
    :data:`WIRE_PRIORITY` for what that means during quiet hours.
    """

    name = "pushover"

    def __init__(
        self,
        app_token: str,
        user_key: str,
        *,
        device: str | None = None,
        sound: str | None = DEFAULT_SOUND,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        url: str = PUSHOVER_URL,
        timeout: float = 10.0,
        user_agent: str = "heli-notifier/0.1 (personal aircraft alerter; contact: local)",
    ) -> None:
        if not app_token.strip():
            raise ValueError("Pushover app token is empty")
        if not user_key.strip():
            raise ValueError("Pushover user key is empty")
        self.app_token = app_token.strip()
        self.user_key = user_key.strip()
        self.device = device
        self.sound = sound
        self.ttl_seconds = ttl_seconds
        self.url = url
        self.timeout = timeout
        self.user_agent = user_agent
        self.sent = 0
        self.failed = 0

    def _payload(self, alert: Alert) -> bytes:
        """Build one uniform notification.

        Every alert is sent identically, including a distress squawk: the same
        sound, the same lifetime, and no repeat-notification. `alert.priority`
        describes the *event* -- it still drives the log, the ordering, and the
        suppress-distress rules in the watcher -- but it deliberately does not
        reach the wire. Pushover's emergency mode (priority 2) re-notifies until
        acknowledged, capped at 3 hours and 50 retries, and that is not wanted
        here: a squawk 7700 is a reason to look up, not a reason to wake
        someone every three minutes for three hours.
        """
        data = {
            "token": self.app_token,
            "user": self.user_key,
            "title": alert.title[:MAX_MESSAGE_CHARS],
            "message": alert.message[:MAX_MESSAGE_CHARS],
            "priority": str(WIRE_PRIORITY),
            "ttl": str(self.ttl_seconds),
        }
        if self.device:
            data["device"] = self.device
        if self.sound:
            data["sound"] = self.sound
        return urllib.parse.urlencode(data).encode("utf-8")


    def verify(self, *, url: str = PUSHOVER_VALIDATE_URL) -> tuple[bool, str]:
        """Check the credentials without sending a message.

        Pushover's users/validate endpoint costs nothing and sends no alert, so
        this can run at startup. The alternative is discovering a bad token when
        an air ambulance is 4 km away and the notification does not arrive, which
        is the worst possible time to find out.
        """
        data = urllib.parse.urlencode({"token": self.app_token, "user": self.user_key}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            url, data=data, headers={"User-Agent": self.user_agent}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            return False, f"Pushover rejected the credentials (HTTP {exc.code})"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, f"could not reach Pushover to verify credentials: {exc}"
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return False, "Pushover returned an unparseable response"
        if not parsed.get("status"):
            errors = parsed.get("errors")
            if isinstance(errors, list):
                errors = ", ".join(errors)
            return False, f"Pushover rejected the credentials: {errors or 'unknown'}"
        return True, "Pushover credentials verified"

    def send(self, alert: Alert) -> bool:
        request = urllib.request.Request(
            self.url,
            data=self._payload(alert),
            headers={"User-Agent": self.user_agent},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            self.failed += 1
            log.error("pushover HTTP %s for %s: %s", exc.code, alert.icao24, exc.reason)
            return False
        except (urllib.error.URLError, TimeoutError, OSError):
            self.failed += 1
            log.exception("could not reach pushover for %s", alert.icao24)
            return False
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            log.warning("pushover returned unparseable body for %s", alert.icao24)
            return False
        if not parsed.get("status"):
            self.failed += 1
            log.error(
                "pushover rejected %s: %s", alert.icao24, parsed.get("errors", "unknown")
            )
            return False
        self.sent += 1
        log.info("sent pushover alert for %s (%s)", alert.icao24, alert.title)
        return True

    def close(self) -> None:
        return None


def is_placeholder(value: str | None) -> bool:
    """True if a credential is empty or still the .env template text."""
    if not value or not value.strip():
        return True
    lowered = value.strip().lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def build_notifier(
    app_token: str | None,
    user_key: str | None,
    *,
    device: str | None = None,
    sound: str | None = DEFAULT_SOUND,
    ttl_seconds: int | None = None,
) -> Notifier:
    """Pick a notifier, refusing to guess when credentials are half-present."""
    if is_placeholder(app_token) and is_placeholder(user_key):
        return LogNotifier()
    if is_placeholder(app_token) or is_placeholder(user_key):
        missing = "app token" if is_placeholder(app_token) else "user key"
        raise ValueError(
            f"Pushover is partly configured: {missing} is missing or still the "
            "placeholder from .env.example. Set both PUSHOVER_TOKEN and "
            "PUSHOVER_USER_KEY, or remove .env to log alerts only."
        )
    if ttl_seconds is not None and ttl_seconds <= 0:
        raise ValueError(f"Pushover TTL must be a positive number of seconds, got {ttl_seconds}")
    return PushoverNotifier(
        app_token,
        user_key,
        device=device,
        sound=sound,
        ttl_seconds=DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds,
    )

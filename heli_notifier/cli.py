"""Command line interface.

Subcommands:
    watch        alert on emergency-services aircraft entering the radius
    notify-test  send one real test alert, to prove the phone works
    healthcheck  exit 0 while the watch loop's heartbeat is fresh (for Docker)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from .adsbdb import AdsbDbClient
from .config import MAX_RADIUS_NM, Config
from .notify import (
    DEFAULT_SOUND,
    DEFAULT_TTL_SECONDS,
    LogNotifier,
    PushoverNotifier,
    build_notifier,
)
from .sources.adsbfi import ADSBFI_BASE_URL, build_client
from .watcher import HEARTBEAT_PATH, Watcher
from .watchlist import WatchConfig


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lat", type=float, help="latitude of the watched position (required; env HELI_NOTIFIER_LAT)"
    )
    parser.add_argument(
        "--lon", type=float, help="longitude of the watched position (required; env HELI_NOTIFIER_LON)"
    )
    parser.add_argument(
        "--radius", type=float, help=f"poll radius in nautical miles (max {MAX_RADIUS_NM:g})"
    )
    parser.add_argument("--data-dir", type=Path, help="directory for the identity-lookup cache")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heli-notifier",
        description="Alert on emergency-services aircraft near a fixed ground position.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_watch = sub.add_parser(
        "watch", help="alert when a watched emergency-services aircraft enters the radius"
    )
    _common(p_watch)
    p_watch.add_argument(
        "--alert-radius",
        type=float,
        help="alert radius in km (default 10; the --radius above only sizes the poll)",
    )
    p_watch.add_argument("--interval", type=int, help="seconds between polls (default 30)")
    p_watch.add_argument(
        "--once", action="store_true", help="poll once, print any alerts, and exit"
    )
    p_watch.add_argument(
        "--dry-run", action="store_true", help="log alerts instead of sending them"
    )
    p_watch.add_argument(
        "--no-lookup", action="store_true", help="skip adsbdb identity lookups entirely"
    )
    p_watch.add_argument("--labels", type=Path, help="JSON file of callsign -> operator name")
    p_watch.add_argument("--device", help="Pushover device name to target")
    p_watch.add_argument("--log-level", default="INFO")

    p_test = sub.add_parser(
        "notify-test", help="send one real test alert, to prove the phone works"
    )
    _common(p_test)
    p_test.add_argument("--device", help="Pushover device to target")

    p_health = sub.add_parser(
        "healthcheck", help="exit 0 while the watch loop's heartbeat is fresh"
    )
    p_health.add_argument(
        "--max-age",
        type=float,
        help="seconds the heartbeat may go stale (default 3x the loop interval)",
    )

    return parser


def _config_from(args, **defaults) -> Config:
    overrides = {
        "lat": getattr(args, "lat", None),
        "lon": getattr(args, "lon", None),
        "radius_nm": getattr(args, "radius", None),
        "data_dir": getattr(args, "data_dir", None),
    }
    overrides.update({k: v for k, v in defaults.items() if v is not None})
    return Config.from_env(**overrides)


def setup_logging(level: str = "INFO") -> None:
    """Log to stderr. The alerter has no log file: it runs in a container whose
    output is already collected by Docker's json-file logging driver."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )


def _watch_config(args) -> WatchConfig:
    """Build the watch config, merging an optional label file over the defaults."""
    config = WatchConfig()
    if getattr(args, "labels", None):
        config = config.with_label_file(args.labels)
    overrides: dict[str, object] = {}
    alert_radius = getattr(args, "alert_radius", None)
    if alert_radius is None:
        raw = os.environ.get("HELI_NOTIFIER_ALERT_RADIUS_KM")
        if raw:
            try:
                alert_radius = float(raw)
            except ValueError:
                raise ValueError(
                    f"HELI_NOTIFIER_ALERT_RADIUS_KM is not a number: {raw!r}"
                ) from None
    if alert_radius:
        overrides["radius_km"] = alert_radius
    if getattr(args, "interval", None):
        overrides["interval_seconds"] = args.interval
    return replace(config, **overrides) if overrides else config


def _pushover_credentials() -> tuple[str | None, str | None]:
    """Read the Pushover app token and user key from the environment.

    Pushover's console calls the second one the "User Key" and the API field is
    `user`, hence PUSHOVER_USER_KEY.
    """
    return os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER_KEY")


def _pushover_ttl() -> int:
    """Seconds an alert stays on the phone, from PUSHOVER_TTL_HOURS.

    Default 12 hours. Pushover's emergency repeat mode is not used, so this is
    the only thing deciding how long any notification lives.
    """
    raw = os.environ.get("PUSHOVER_TTL_HOURS")
    if not raw or not raw.strip():
        return DEFAULT_TTL_SECONDS
    try:
        hours = float(raw)
    except ValueError:
        raise ValueError(f"PUSHOVER_TTL_HOURS is not a number: {raw!r}") from None
    return int(hours * 3600)


def _print_problems(problems: list[str]) -> None:
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)


def cmd_watch(args) -> int:
    try:
        config = _config_from(args)
        watch_config = _watch_config(args)
        notifier = build_notifier(
            *_pushover_credentials(),
            device=args.device or os.environ.get("PUSHOVER_DEVICE") or None,
            sound=os.environ.get("PUSHOVER_SOUND", DEFAULT_SOUND),
            ttl_seconds=_pushover_ttl(),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    problems = config.validate()
    if problems:
        _print_problems(problems)
        return 2

    # A live run with no credentials logs instead of notifying, which looks
    # identical to a working setup right up until a helicopter is overhead and
    # nothing arrives. Say so, once, before the first poll.
    if isinstance(notifier, LogNotifier) and not args.dry_run:
        print(
            "warning: no Pushover credentials, alerts are being written to the log "
            "only. Set PUSHOVER_TOKEN and PUSHOVER_USER_KEY in .env, or pass "
            "--dry-run if that is intended.",
            file=sys.stderr,
        )

    # Verify the token before going live. A credential that is wrong, revoked, or
    # still the .env placeholder fails here rather than silently at 3am.
    if isinstance(notifier, PushoverNotifier):
        ok, message = notifier.verify()
        if not ok:
            print(f"error: {message}", file=sys.stderr)
            return 2
        print(message)

    setup_logging(args.log_level)
    watcher = Watcher(
        lat=config.lat,
        lon=config.lon,
        config=watch_config,
        notifier=notifier,
        dry_run=args.dry_run,
    )
    watcher.bind(
        build_client(config, ADSBFI_BASE_URL),
        None if args.no_lookup else AdsbDbClient(config.adsbdb_cache_path),
    )

    print(
        f"watching {watch_config.radius_km:g} km around {config.lat:.5f}, "
        f"{config.lon:.5f} every {watch_config.interval_seconds}s via {notifier.name}"
    )
    print(
        "  matching HEMS "
        + "/".join(watch_config.hems_prefixes)
        + ", police "
        + "/".join(watch_config.police_prefixes)
        + ", squawk "
        + "/".join(
            sorted(watch_config.hems_squawks | watch_config.police_squawks)
        )
        + ", distress squawk "
        + "/".join(sorted(watch_config.emergency_squawks))
    )
    try:
        stats = watcher.run(once=args.once)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(stats.summary())
    if getattr(notifier, "failed", 0):
        print(f"  {notifier.failed} notification(s) failed to send", file=sys.stderr)
    return 0


def cmd_notify_test(args) -> int:
    """Send one real alert-shaped notification, without polling anything.

    Installing the alerter and installing the *notifications* are different
    problems. A green startup log only proves the token was accepted, not that
    the message reaches the phone, is audible, and survives quiet hours. The
    first genuine aircraft could be hours away, so this sends a representative
    alert now instead of discovering the gap at 02:00.

    The body is a real alert rendered from a synthetic match, so what arrives is
    the same shape the user will see later. It is labelled as a test so nobody
    goes outside to look for a helicopter.
    """
    from dataclasses import replace as _replace
    from datetime import datetime, timezone

    from .alerts import build_alert
    from .watchlist import classify

    try:
        notifier = build_notifier(
            *_pushover_credentials(),
            device=getattr(args, "device", None) or os.environ.get("PUSHOVER_DEVICE") or None,
            sound=os.environ.get("PUSHOVER_SOUND", DEFAULT_SOUND),
            ttl_seconds=_pushover_ttl(),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if isinstance(notifier, LogNotifier):
        print(
            "error: no usable Pushover credentials, so this would only log.\n"
            "       Set PUSHOVER_TOKEN and PUSHOVER_USER_KEY in .env, then fill in\n"
            "       the real values; a placeholder is treated as missing on purpose.",
            file=sys.stderr,
        )
        return 2

    ok, message = notifier.verify()
    if not ok:
        print(f"error: {message}", file=sys.stderr)
        return 2

    # A watchlist match with no live aircraft behind it: 8 km out and inbound
    # on the same heading as the bearing, so the closing speed is positive and
    # the "time to overhead" line appears. That is the case worth rehearsing --
    # a receding aircraft has no ETA and is the least interesting shape to
    # confirm you can actually see on a phone.
    match = _replace(
        classify("HLE22", None, 8.0, WatchConfig()),
        priority=0,
    )
    alert = build_alert(
        match,
        icao24="TEST01",
        when=datetime.now(tz=timezone.utc),
        distance_km=8.0,
        bearing_deg=225.0,
        baro_alt_ft=1500,
        ground_speed_kt=120.0,
        track_deg=225.0,
        registration="G-TEST",
        type_name="TEST - not a real aircraft",
    )
    # The marker goes on its own line. Written as a real escape, not a doubled
    # backslash: "TEST\\n" in source is a literal backslash and n, which ships
    # to the phone as the two characters \n.
    alert = _replace(
        alert,
        title=f"TEST - {alert.title}",
        message=f"TEST\n{alert.message}",
    )

    if notifier.send(alert):
        print("sent. the alert above is what a real one looks like.")
        return 0
    print(
        "error: Pushover rejected the test message; the alerter will not be able to\n"
        "       notify either. Check the token, the user key, and the device name.",
        file=sys.stderr,
    )
    return 1


def cmd_healthcheck(args) -> int:
    """Exit 0 while the watch loop's heartbeat is fresh, 1 otherwise.

    The heartbeat is written by the watcher after every poll, so this is a
    liveness probe for the loop itself: it answers "is the alerter still
    turning over", not "is the network up" and not "did an alert arrive".
    Docker runs it via HEALTHCHECK; the exit code is the only contract.
    """
    path = HEARTBEAT_PATH
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"unhealthy: no heartbeat at {path}", file=sys.stderr)
        return 1
    parts = raw.split()
    try:
        stamp = float(parts[0])
        interval = float(parts[1]) if len(parts) > 1 else 30.0
    except (ValueError, IndexError):
        print(f"unhealthy: unreadable heartbeat at {path}: {raw!r}", file=sys.stderr)
        return 1
    max_age = args.max_age if args.max_age is not None else max(3.0 * interval, 90.0)
    age = time.time() - stamp
    if age > max_age:
        print(f"unhealthy: heartbeat is {age:.0f}s old (max {max_age:.0f}s)", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "watch": cmd_watch,
        "notify-test": cmd_notify_test,
        "healthcheck": cmd_healthcheck,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

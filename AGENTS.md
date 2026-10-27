# AGENTS.md

Emergency-services aircraft alerter for a fixed site position, running 24/7
on a Raspberry Pi. Python standard library only — **no third-party runtime
dependencies, by design.**
Do not add one without a very good reason; the "works on a Pi with no build
step" property is the point.

## Commands

```bash
docker compose up -d --build
docker compose logs -f
docker compose ps
docker compose down
docker compose down -v
docker compose run --rm heli-notifier notify-test
PYTHONPATH=. python3 -m unittest discover -s tests -t .
```

Run tests directly when iterating (note `PYTHONPATH`; the package is not
installed):

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -t .
PYTHONPATH=. python3 -m unittest tests.test_watch          # one module
```

One-off containers, in the foreground:

```bash
./run.sh --once --dry-run     # one poll, logged only, phone untouched
./run.sh notify-test          # one real test alert
```

`ruff` config is in `pyproject.toml` (line-length 100, `E,F,W,I,UP,B`) but ruff
is not installed on the Pi or the dev box. Don't claim lint is clean.

## Deployment facts that are easy to get wrong

**Always include `--build` when starting after code changes.** Plain
`docker compose up -d` can reuse the existing image, so the container may keep
running old code. Use `docker compose up -d --build` to ensure the image is
rebuilt before it starts.

**`.env` is required and holds the only site data and secrets** (the watched
position in `HELI_NOTIFIER_LAT`/`LON`, plus the two Pushover credentials).
`compose.yaml` loads it via `env_file`, so a missing `.env` fails the compose
run loudly at startup. That is deliberate: a missing credential should never
look like a working alerter. `.env` is gitignored and must stay out of the
image (`.dockerignore` covers it).

**Coordinates are site data and never enter the repo.** No default position in
`config.py`, none in `compose.yaml`, none in tests, none in docs -- the
position comes from `.env` or `--lat`/`--lon` and `validate()` names the
missing variable. Do not "helpfully" add a working default: publishing
somebody's home coordinates is the one leak this project cannot undo.

**State is a named volume, not a bind mount.** `heli-notifier-data` (mounted at
`/data`) holds the adsbdb identity cache. Named volumes inherit the image's
ownership on first use, which is why the Dockerfile no longer takes UID/GID
build args: there is no host directory to go root-owned. `run.sh` refers to the
volume by its fixed name (`name: heli-notifier-data` in compose) so one-off runs
share the cache with the long-running service.

**The container runs as a fixed uid/gid 1000.** The Dockerfile creates the
`heli` user itself; nothing is adopted from a host id.

**Detached, and restart-safe.** `docker compose up -d --build` runs detached, so
logging off does not stop it, and `restart: unless-stopped` brings it back after
a reboot. There is no database and no deadline to persist: a restart re-acquires
whatever is inside the radius on the next poll.

**Expected log cadence.** The watcher logs a stats line on the first poll, then
only every 20th poll (`SUMMARY_EVERY_POLLS` in `watcher.py`) ≈ one line per
10 min. Silence between lines is normal, not a hang. `./run.sh` and
`docker compose run` are one-shot/foreground and exit when you stop them.

**Container health is loop liveness only.** The image has a `HEALTHCHECK` that
runs `python -m heli_notifier.cli healthcheck`: it reads a heartbeat file
(`/tmp/heli-notifier-heartbeat`) the watcher rewrites after *every* poll,
including failed ones, and goes unhealthy once it is older than 3× the
interval. It answers "is the alerter still turning over" -- not "is the
network up", not "did an alert arrive"; feed and Pushover failures live in the
stats line. A failed poll must still write the heartbeat (an outage is not a
hang), and a heartbeat write failure must never crash the watcher.

## Code layout

- `heli_notifier/config.py` — where we are and how wide we poll: `Config` (lat,
  lon with no default, poll radius, data dir). Env vars + CLI overrides; flag
  wins. Position is site data, see above.
- `heli_notifier/sources/adsbfi.py` — the only external data feed (HTTP).
- `heli_notifier/geo.py` — haversine, bearing, 16-wind compass. Nothing else.
- `heli_notifier/watchlist.py` — what counts as an emergency-services aircraft,
  the local operator label table, and `classify()`. Squawk sets and prefix sets
  are module constants so the alerter and its own descriptions cannot disagree.
- `heli_notifier/watcher.py` — the poll loop and the entry/exit state machine.
- `heli_notifier/alerts.py` — renders a `Match` into the notification text.
- `heli_notifier/notify.py` — `LogNotifier` (default) and `PushoverNotifier`.
  `build_notifier()` treats an unfilled `.env` as no credentials, raises when
  only one of the pair is set, and `verify()` checks the token against
  Pushover's `users/validate` at startup so a bad token cannot hide until an
  alert is missed. `PUSHOVER_USER_KEY` is the only name read; there is no
  `PUSHOVER_KEY` alias, so a stale `.env` errors instead of silently logging.
- `heli_notifier/adsbdb.py` — registration/type lookup, cached and rate limited.
- `heli_notifier/cli.py` — `watch` / `notify-test` / `healthcheck`.

## Alerter invariants

These rules decide whether a phone stays switched on. `tests/test_watch.py`
pins each one; do not "simplify" them away.

- **Entry and exit are different radii.** `--alert-radius` (10 km) gates a new
  alert; `exit_hysteresis` (1.15, so 11.5 km) decides when an aircraft is
  believed gone. Collapsing them either delays the alert or re-fires on
  boundary wobble. The poll radius (`--radius`, 15 NM ≈ 28 km) must stay above
  the exit radius or aircraft drop out of the feed while still overhead.
- **Closing is not news.** `classify()` returns a *higher priority* inside
  5 km, and priority must never be used to decide whether to re-alert. An
  aircraft at 9 km becoming 4 km is the same arrival. Escalation fires only
  when an aircraft already inside starts squawking distress.
- **A dropped poll is not a departure.** `_tracked` entries survive
  `gap_polls` (5) consecutive misses. Popping them on the first miss made every
  signal gap look like an arrival.
- **One incident, one notification**, keyed on callsign not airframe:
  `duplicate_window_seconds` (600). Separate airframes sharing one callsign
  must not generate duplicate notifications for the same incident.
- **`on_ground` is checked before anything else.** A parked air ambulance wears
  a watched callsign.
- **Distress is never suppressed** — not by the duplicate window, not by the
  two-hour loiter reminder.
- **Labels are by callsign**, because one callsign is flown by more than one
  airframe. `with_label_file()` returns a new config and must not mutate
  `LOCAL_SERVICE_LABELS`.
- **Cache stamps use `time.time()`, pacing uses `time.monotonic()`.** The cache
  outlives the process; a persisted monotonic value reads as permanently fresh
  after a restart.
- **Every notification is identical: `sound=siren`, `ttl=43200`,
  `priority=0`, no repeat.** `alert.priority` describes the *event* and still
  drives the log, the ordering, and the suppress-distress rules, but it never
  reaches the wire. Pushover `priority=2` is deliberately unused: it re-sounds
  until acknowledged (3 h / 50 retries max) and would get the notification
  muted. `priority=0` is an explicit choice, not an oversight: it means Pushover
  silences arrivals during the user's quiet hours, and the agreed fix for that
  is the user adjusting their own quiet hours, not the alerter raising priority
  for every alert. Do not reintroduce retry/expire or an emergency branch in
  `_payload`. `tests/test_watch.py` pins this, including a check that no alert
  emits `retry` or `expire`.
- Keep alert volume in mind when changing radii, matching rules, or duplicate
  suppression; small changes can cause many more notifications.

## adsb.fi API specifics

Payload key is **`ac`**, not `aircraft`; the client raises if it's missing.
`alt_baro` is sometimes the *string* `"ground"` — coerced to `None`, and
`on_ground` treats `None` baro altitude as ground. Optional fields (`gs`,
`lat`/`lon`) may be null and must be skipped, not assumed. Feed is unmetered
and unauthenticated; the 1 req/s limit is why the default interval is 30s.
Base URL is `https://opendata.adsb.fi/api`. Personal/non-commercial use only —
keep the attribution in the README.

## Conventions

- `from __future__ import annotations` at the top of every module.
- Type hints on public functions; `float` where a unit conversion or fractional
  duration is possible.
- Comments explain *why*, not *what*. The repo is comment-heavy by intent —
  match that, and keep comments honest with the code.
- The alerter must never die on a single bad poll; failures are counted in
  `stats` and the loop continues. Keep that.
- `dry_run` must send nothing and touch no phone; alerts go through
  `LogNotifier`-style logging only.
- Attribution to adsb.fi is a licensing requirement, not decoration.

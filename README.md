# heli-notifier

Aircraft alerter for a fixed ground position. It polls the adsb.fi open data
feed and sends a Pushover notification when an emergency-services aircraft --
air ambulance, police, or anything squawking distress -- comes inside the alert
radius.

The alert is built to answer one question: **which way do I look**.

```
HLE22 - 7.6 km SSW
Wiltshire and Bath Air Ambulance
1,500 ft, 120 kt, +118 kt closing
2 min to overhead
G-WOAC - Airbus H125
```

The 10 km alert radius keeps routine activity at a base further out silent,
while alerting when a relevant aircraft comes nearby.

## Design constraints

**No third-party Python dependencies.** The project runs on the standard
library alone: `urllib` for HTTP and `math` for geometry. On a Raspberry Pi
this means no build step, no wheels, nothing to go stale, and an image that is
just CPython plus this source.

**Alerting is entry-triggered.** An aircraft raises a notification when it
crosses *into* the radius, not on every poll it is inside. The alternative --
"inside" rather than "entered" -- produced 479 notifications for one police
helicopter that spent four hours over the area.

**One incident, one notification.** Air ambulance services routinely fly two
airframes on one job under a single callsign, so duplicates are keyed on the
callsign. Nothing re-notifies, including a distress squawk: Pushover's
emergency mode repeats for up to three hours, and a siren that will not stop
is how an alert gets muted for good.

## Quick start

```bash
cp .env.example .env && $EDITOR .env   # your position, then Pushover credentials
docker compose up -d --build           # build and start, detached
docker compose logs -f                 # follow it
```

Keep `--build` when restarting after code changes; otherwise Compose can reuse
the existing image and continue running old code.

`.env` is required. It holds your position (`HELI_NOTIFIER_LAT`/`LON` -- there
is no default in the source, and coordinates never appear in the repo) and the
two Pushover credentials. If it is missing, compose fails loudly rather than
starting an alerter that quietly logs instead of notifying, which is the
failure that matters.

Before trusting it unattended, prove the phone actually buzzes:

```bash
docker compose run --rm heli-notifier notify-test  # one real test alert, labelled TEST
```

## Running without Docker

Requires Python 3.12 or newer. No installation step is needed.

```bash
set -a; source .env; set +a           # export your position and credentials
python -m heli_notifier.cli watch --dry-run      # alert, log only, no phone
python -m heli_notifier.cli watch --once         # one poll, then exit
python -m heli_notifier.cli notify-test          # send one real test alert
python -m unittest discover -s tests -t .     # run the tests (no .env needed)
```

For a one-off container in the foreground, `./run.sh` wraps the same image and
defaults the subcommand to `watch`:

```bash
./run.sh --once --dry-run    # one poll, logged only, phone untouched
./run.sh                     # run until you stop it with Ctrl-C
```

## Pushover setup

`.env` holds your position and the two credentials, is gitignored and chmod
600. Get the credentials from
<https://pushover.net>: create an application for the **App Token**, and the
**User Key** is on your dashboard. `.env.example` is the template.

```bash
PUSHOVER_TOKEN=replace-with-your-app-token
PUSHOVER_USER_KEY=replace-with-your-user-key
```

Three things fail loudly rather than quietly, because a notification that does
not arrive is the one failure that matters:

* **An unfilled `.env` is treated as no credentials.** The placeholder text is
  rejected outright, so you get log-only output instead of a notifier that
  401s on every alert.
* **Half a configuration is a startup error.** Setting the token but not the
  user key exits with a message naming the missing one.
* **The token is verified at startup** against Pushover's `users/validate`
  endpoint, which sends no message. A wrong or revoked token is caught when the
  service starts, not at 3am when an air ambulance is 4 km away.

Only `PUSHOVER_USER_KEY` is read. There is deliberately no `PUSHOVER_KEY`
alias: a name that used to work and silently stopped working is worse than a
startup error.

### Sound and lifetime

Every alert is sent identically:

```
sound=siren   ttl=43200   priority=0
```

* **`siren`** on every alert, so it is audible over traffic and from another
  room while asleep. Override with `PUSHOVER_SOUND`, or set it to `none`.
* **12-hour `ttl`**, meaning "delete this from the phone after 12 hours". An
  alert that old is stale, and leaving it in the shade invites acting on old
  news. Override with `PUSHOVER_TTL_HOURS`.
* **`priority=0`**, Pushover's normal priority.

One consequence to be aware of: Pushover downgrades a normal-priority
notification to **no sound and no alert** during your configured quiet hours.
If a night-time arrival needs to make noise, widen or disable quiet hours in
the Pushover app. That is a per-user setting, and it seemed better to leave it
under your control than to have the alerter override it for every alert.

**Nothing re-notifies, including a distress squawk.** The distinction between a
routine arrival and a distress squawk is still kept where it is useful -- the
alerter titles it `EMERGENCY - ...`, still suppresses duplicate alerts
differently, and still shows `squawk 7700` in the body. It just does not change
how the notification is delivered.

## What counts as watched

An aircraft is watched when its callsign starts with `HLE` (UK HEMS), `HM`
(Helimed) or `UKP` (police), when it squawks `0020` (HEMS) or `0061`-`0064`
(police), or when it squawks `7700`/`7500`/`7600` from any operator. The last
group is a distress signal rather than a service, so it is never suppressed.

Operator names come from a local label table in `heli_notifier/watchlist.py`,
keyed by exact callsign, overridable with `--labels`. Kept by callsign rather
than registration because one callsign is flown by more than one airframe:
`HLE65` was `G-GWAC` and `G-EMSS` on the same day.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--alert-radius` | `10` | Radius in km that raises an alert |
| `--radius` | `15` | Radius in NM that sizes the adsb.fi poll |
| `--interval` | `30` | Seconds between polls |
| `--once` | | Poll once, print, and exit |
| `--dry-run` | | Log alerts instead of sending them |
| `--no-lookup` | | Skip adsbdb identity lookups |
| `--labels` | | JSON file of callsign to operator name |
| `--device` | `PUSHOVER_DEVICE` | Pushover device to target |

`--radius` sizes the feed query and only has to cover the alert radius plus
its exit hysteresis (10 km x 1.15 = 11.5 km = 6.2 NM); `--alert-radius` is what
the alerter acts on. They are separate on purpose so a wide poll and a narrow
alert can coexist -- raise one and remember to raise the other.

### Keeping the volume honest

A phone notification that fires too often is worse than none, so the alerter
only speaks up in specific situations:

* **On entry, once.** A second and third poll of the same aircraft staying
  inside is not news. An aircraft that flies out past 11.5 km and returns
  alerts again, because that genuinely is a new arrival.
* **Not on approach.** Closing from 9 km to 4 km is the same aircraft getting
  nearer. This was a real bug: the first version treated a priority change as
  news and doubled every notification.
* **Once per incident, not per airframe.** A second airframe within ten
  minutes on the same callsign is suppressed, because it is the same job.
* **After two hours of loitering.** One notification is right, but if it is
  still there two hours later you may have missed the first.
* **Never for a distress squawk.** Emergencies are never suppressed by the
  duplicate window or the loiter rule.
* **Never for aircraft on the ground.** A helicopter parked on a pad at its
  base wears a watched callsign and stays silent.

## Configuration

The settings below are available as command line flags and, where shown, as
environment variables (the position has to be one or the other; `--interval`
and the behaviour flags are flags only). The flag wins over the environment.

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--lat` | `HELI_NOTIFIER_LAT` | required | Watched position latitude |
| `--lon` | `HELI_NOTIFIER_LON` | required | Watched position longitude |
| `--radius` | `HELI_NOTIFIER_RADIUS_NM` | `15` | Poll radius in nautical miles (max 250) |
| `--alert-radius` | `HELI_NOTIFIER_ALERT_RADIUS_KM` | `10` | Radius in km that raises an alert |
| `--interval` | | `30` | Seconds between polls |
| `--data-dir` | `HELI_NOTIFIER_DATA_DIR` | `./data` | Identity-lookup cache |

## Operating notes

**Logging off does not stop the alerter.** `docker compose up -d --build` starts
a detached container with `restart: unless-stopped`, so it keeps running when
you disconnect and comes back on its own after a reboot.

**A restart loses nothing.** The alerter keeps no database: the state machine
is about which aircraft are currently inside the radius, and a restart simply
re-acquires them on the next poll. The adsbdb identity cache lives in a named
Docker volume (`heli-notifier-data`), so registrations are not re-fetched either.

**The log cadence is deliberately sparse.** A poll summary is printed on the
first poll and then every 20th (about one line per 10 minutes). Silence
between those lines is normal, not a hang.

**`docker ps` shows `healthy`/`unhealthy`.** The image carries a healthcheck
that reads a heartbeat file the watcher rewrites after every poll (including
failed ones), so `unhealthy` means the loop itself stopped turning over --
not that the network or Pushover is down, which the log's stats line reports.

```bash
docker compose logs -f       # follow it
docker compose ps            # container status
docker compose down          # stop, keep the identity cache
docker compose down -v       # stop and delete the identity cache volume
```

## Data source

Aircraft positions come from the [adsb.fi](https://adsb.fi) open data feed:

```
GET https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}
```

It is a radius query, so the distance filter is applied server-side. The
endpoint needs no API key, has no daily quota, and the documented public limit
is one request per second, which a 30 second cadence respects by a wide margin.
A descriptive `User-Agent` is sent, as adsb.fi asks consumers to be
identifiable.

Two behaviours of the feed that the parser handles explicitly:

* `alt_baro` is sometimes the **string** `"ground"` rather than a number.
  Passing that to `int()` raises `ValueError` and would abort a poll, so every
  numeric field goes through a coercion helper. A null baro altitude is treated
  as "on the ground", which is what keeps a parked air ambulance silent.
* `alt_geom`, `track`, and `baro_rate` are absent on some aircraft, so no
  field is assumed to be present. Aircraft with no position are skipped.

Registration and aircraft type come from a second lookup against
[adsbdb](https://adsbdb.com), cached to disk and rate limited. A lookup
failure never delays or suppresses an alert -- the notification goes out with
whatever the feed already gave us.

## Attribution

Aircraft data is provided by [adsb.fi](https://adsb.fi), free for personal,
non-commercial use. `heli-notifier` is an independent project and is not
affiliated with or endorsed by adsb.fi.

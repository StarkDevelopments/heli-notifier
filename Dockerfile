# syntax=docker/dockerfile:1
#
# Single-stage build, deliberately. The project has no third-party Python
# dependencies, so there is nothing to compile, vendor, or install: the
# standard library is the entire runtime. That keeps the image small, removes
# any wheel or supply-chain risk, and means the same Dockerfile works for
# linux/arm64 on a Raspberry Pi and for linux/amd64 during development.

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HELI_NOTIFIER_DATA_DIR=/data

# A fixed non-root user. State is a named Docker volume, not a bind mount, so
# there is no host uid/gid to match: the volume inherits this ownership on
# first use and the process can write to it.
RUN set -eux; \
    groupadd --gid 1000 heli; \
    useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin heli; \
    mkdir -p /data /app; \
    chown -R heli:heli /data /app

WORKDIR /app

COPY --chown=1000:1000 heli_notifier/ /app/heli_notifier/
COPY --chown=1000:1000 pyproject.toml README.md /app/

USER 1000:1000

# Build-time verification: importing the entry point and its dependencies
# catches a syntax error or a bad import here, at build time, rather than as a
# dead alerter on the Pi at 2am.
RUN set -eux; \
    python -c "import heli_notifier.cli, heli_notifier.geo, heli_notifier.watcher, heli_notifier.watchlist, heli_notifier.alerts, heli_notifier.notify, heli_notifier.adsbdb, heli_notifier.sources.adsbfi; print('imports ok')"; \
    python -m heli_notifier.cli --help > /dev/null; \
    python -VV

# The alerter runs until stopped: one long-lived process, restarted by Docker
# after a reboot or a crash. A crash is already covered by `restart:
# unless-stopped`; what no restart policy can see is a process that is alive
# but no longer looping (a wedged interpreter, a stuck lock). The watcher
# writes a heartbeat file after every poll, and the healthcheck below fails
# once it goes stale, so `docker ps` shows `unhealthy` instead of a silently
# dead alerter.
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "heli_notifier.cli"]
CMD ["watch"]

# Liveness of the poll loop only: it does not check the network, the feed, or
# Pushover -- those are counted in the stats line, not in container health.
# start-period covers the initial Pushover credential check plus a few polls;
# interval matches the default poll cadence; retries=3 means ~90s of grace
# beyond the heartbeat's own staleness threshold before the state flips.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-m", "heli_notifier.cli", "healthcheck"]

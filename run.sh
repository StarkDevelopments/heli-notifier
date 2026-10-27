#!/usr/bin/env bash
# Run the alerter in a throwaway container, in the foreground.
#
# This is for one-off runs and checks. The always-on alerter is started with
# `docker compose up -d --build`, which detaches and restarts after a reboot;
# this script starts a container that lives only as long as the terminal session.
#
#   ./run.sh --once --dry-run       # one poll, logged only, phone untouched
#   ./run.sh --once                 # one poll, notifying
#   ./run.sh                        # run until you stop it with Ctrl-C
#   ./run.sh notify-test            # send one real test alert
#
# Position and credentials come from .env, which is required. Build the image
# first with `docker compose build`.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${HELI_NOTIFIER_IMAGE:-heli-notifier:latest}"

if [ ! -f .env ]; then
  echo "missing .env - copy .env.example to .env and set your position and credentials" >&2
  exit 1
fi

# The image entrypoint is `python -m heli_notifier.cli`, which needs a subcommand
# before any flags. Default to `watch` when the caller gives flags only or
# nothing at all, but pass a real subcommand straight through.
case "${1:-}" in
  watch|notify-test) ;;
  *) set -- watch "$@" ;;
esac

# Shares the identity-lookup cache volume with the compose service, so a
# one-off run does not re-fetch every registration the long-running alerter
# has already looked up.
exec docker run --rm \
  -v heli-notifier-data:/data \
  --env-file .env \
  -e HELI_NOTIFIER_DATA_DIR=/data \
  "$IMAGE" "$@"

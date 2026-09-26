#!/bin/sh
set -eu

if [ -z "${NJULINK_API_KEY:-}" ] && [ -s /run/secrets/njulink_api_key ]; then
  NJULINK_API_KEY=$(tr -d '\r\n' < /run/secrets/njulink_api_key)
  export NJULINK_API_KEY
fi

run_uid="${HARNESS_UID:-1000}"
run_gid="${HARNESS_GID:-1000}"
export HOME=/tmp/harness-home
mkdir -p "$HOME" "$UV_CACHE_DIR"

if [ "$(id -u)" -eq 0 ]; then
  chown "$run_uid:$run_gid" "$HOME" "$UV_CACHE_DIR"
  exec setpriv --reuid="$run_uid" --regid="$run_gid" --clear-groups "$@"
fi

exec "$@"

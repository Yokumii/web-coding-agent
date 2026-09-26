#!/bin/sh
set -eu

if [ "$#" -lt 2 ]; then
  echo "usage: $0 /data/path/to/batch_plan.json /data/path/to/output [extra batch args...]" >&2
  exit 2
fi

plan=$1
output=$2
shift 2

export DOCKER_HOST="${DOCKER_HOST:-unix:///var/run/docker.sock}"
export HARNESS_DATA_ROOT="${HARNESS_DATA_ROOT:-/data2/adminweihunj/webcoding/WebCoding_Data}"
export NJULINK_KEY_FILE="${NJULINK_KEY_FILE:-/home/adminweihunj/.config/webcoding/credentials/njulink-luna.key}"
export HTTP_PROXY="${HTTP_PROXY:-http://127.0.0.1:7890}"
export HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:7890}"
export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1}"
export HARNESS_UID="${HARNESS_UID:-$(id -u)}"
export HARNESS_GID="${HARNESS_GID:-$(id -g)}"

docker compose run --rm harness \
  .venv/bin/python -u scripts/run_g2_compound_batch.py \
  --plan "$plan" \
  --output "$output" \
  --limit "${HARNESS_CASES:-8}" \
  --workers "${HARNESS_WORKERS:-8}" \
  --base-port "${HARNESS_BASE_PORT:-19600}" \
  --model gpt-5.6-luna \
  --review-model gpt-5.6-luna \
  --provider-profile openai \
  --production-atomic-contract \
  --no-rsi \
  --budget-usd "${HARNESS_CASE_BUDGET_USD:-20}" \
  --request-timeout "${HARNESS_REQUEST_TIMEOUT:-300}" \
  --case-timeout "${HARNESS_CASE_TIMEOUT:-2400}" \
  --debug-max-rounds "${HARNESS_MAX_REPAIR_ROUNDS:-10}" \
  --reverse-validate-root /app/reverse/validate \
  --reverse-node node \
  --reverse-playwright /opt/reverse-validator/node_modules/playwright \
  --reverse-extra-node-modules /opt/reverse-validator/node_modules \
  "$@"

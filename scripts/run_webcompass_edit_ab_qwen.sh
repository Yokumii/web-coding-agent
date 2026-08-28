#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
run_stamp="$(date +%Y%m%dT%H%M%S)"
log_root="$repo_root/logs/webcompass_edit_ab/qwen_$run_stamp"
mkdir -p "$log_root"

source "$repo_root/scripts/qwen_env.sh"
unset ALL_PROXY HTTPS_PROXY HTTP_PROXY all_proxy https_proxy http_proxy
export SSL_NO_VERIFY=1
export EVALUATOR_VISION_MAX_RETRIES=0

cd "$repo_root"
set +e
uv run python scripts/run_webcompass_edit_ab.py \
  --output-root "$log_root" \
  "$@" 2>&1 | tee "$log_root/console.log"
status="${PIPESTATUS[0]}"
set -e
printf '%s\n' "$status" > "$log_root/exit_status.txt"
exit "$status"

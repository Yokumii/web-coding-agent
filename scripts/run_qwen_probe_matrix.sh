#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
run_id="${1:-$(date +%Y%m%d_%H%M%S)}"
log_dir="$repo_root/logs/qwen_probe/$run_id"
mkdir -p "$log_dir"
source "$repo_root/scripts/qwen_env.sh"

printf '%s\n' "qwen3.6-plus probe matrix: direct/proxy x stream/nonstream" > "$log_dir/command.txt"
printf '%s\n' "SSL_NO_VERIFY=${SSL_NO_VERIFY:-}" > "$log_dir/environment.txt"
: > "$log_dir/results.jsonl"

run_probe() {
  local route="$1"
  local mode="$2"
  local output="$log_dir/${route}_${mode}.log"
  if [[ "$route" == "proxy" ]]; then qwen_proxy_on; else qwen_proxy_off; fi
  local started
  started="$(date +%s)"
  if [[ "$mode" == "stream" ]]; then
    uv run python "$repo_root/scripts/probe_qwen_api.py" --stream > "$output" 2>&1
  else
    uv run python "$repo_root/scripts/probe_qwen_api.py" > "$output" 2>&1
  fi
  local exit_code=$?
  local elapsed=$(( $(date +%s) - started ))
  printf '{"route":"%s","mode":"%s","status":"%s","exit_code":%d,"elapsed_seconds":%d,"log":"%s"}\n' \
    "$route" "$mode" "$([[ $exit_code -eq 0 ]] && printf ok || printf error)" \
    "$exit_code" "$elapsed" "$output" >> "$log_dir/results.jsonl"
}

run_probe direct nonstream
run_probe direct stream
run_probe proxy nonstream
run_probe proxy stream
printf '%s\n' "$log_dir"

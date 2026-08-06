#!/usr/bin/env bash
# Run one resumable forward harness case. Start it with nohup for long runs.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <workdir> <prompt-file> <plan|resume>" >&2
  exit 2
fi

workdir="$1"
prompt_file="$2"
phase="$3"
if [[ ! -f "$prompt_file" ]]; then
  echo "prompt file missing: $prompt_file" >&2
  exit 2
fi

source "$(dirname "$0")/qwen_env.sh"
# A full case remains quality-gated, but cap tool turns for calibration runs so
# a single meandering agent cannot consume an unbounded share of the batch.
export AGENT_MAX_TOOL_CALLS="${FORWARD_MAX_TOOL_CALLS:-80}"
max_budget="${FORWARD_MAX_BUDGET:-6}"
qwen_proxy_on
if ! nc -z 127.0.0.1 "$QWEN_PROXY_PORT"; then
  echo "proxy_unavailable: socks5://127.0.0.1:${QWEN_PROXY_PORT}" >&2
  exit 3
fi

case_id="$(basename "$workdir")"
run_id="$(date +%Y%m%dT%H%M%S)"
agent_root="$(cd "$(dirname "$0")/.." && pwd)"
data_root="${WEB_CODING_DATA_ROOT:-$(cd "$agent_root/.." && pwd)}"
log_root="${WEB_CODING_AGENT_LOG_ROOT:-$data_root/logs/agentic}"
log_dir="$log_root/forward_harness/${case_id}/${run_id}"
mkdir -p "$log_dir"
printf 'status=started\nphase=%s\nworkdir=%s\nprompt_file=%s\nmodel=%s\n' \
  "$phase" "$workdir" "$prompt_file" "$GENERATOR_MODEL" > "$log_dir/run_metadata.txt"

prompt="$(<"$prompt_file")"
case "$phase" in
  plan)
    uv run python -m src.main "$prompt" --workdir "$workdir" --keep-frontend \
      --plan-only --max-rounds 4 --max-budget "$max_budget" --planner-scope-mode query-aligned \
      --playwright-headless 2>&1 | tee "$log_dir/harness.log"
    ;;
  resume)
    uv run python -m src.main "$prompt" --workdir "$workdir" --resume \
      --max-rounds 4 --max-budget "$max_budget" --planner-scope-mode query-aligned \
      --playwright-headless 2>&1 | tee "$log_dir/harness.log"
    ;;
  *)
    echo "phase must be plan or resume" >&2
    exit 2
    ;;
esac
printf 'status=finished\n' >> "$log_dir/run_metadata.txt"

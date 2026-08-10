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

# A workdir is one trajectory: concurrent resumes can interleave commits and
# traces, destroying provenance even when both individual processes succeed.
lock_dir="$workdir/.forward_runner_lock"
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "workdir_locked: another forward runner may still own $workdir" >&2
  exit 4
fi
finished=0
log_metadata=""
cleanup_lock() {
  rc=$?
  if [[ -n "$log_metadata" ]]; then
    if [[ "$finished" -eq 1 ]]; then
      printf 'status=finished\nexit_code=0\n' >> "$log_metadata"
    else
      # Preserve the original trajectory and log. This status makes a quota,
      # timeout, or tool failure visible to batch resume logic instead of
      # looking like a completed data example.
      printf 'status=failed\nexit_code=%s\n' "$rc" >> "$log_metadata"
    fi
  fi
  rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup_lock EXIT

source "$(dirname "$0")/qwen_env.sh"
# A full case remains quality-gated, but cap tool turns for calibration runs so
# a single meandering agent cannot consume an unbounded share of the batch.
export AGENT_MAX_TOOL_CALLS="${FORWARD_MAX_TOOL_CALLS:-48}"
export AGENT_PHASE_TIMEOUT_SECONDS="${FORWARD_AGENT_PHASE_TIMEOUT_SECONDS:-240}"
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
log_metadata="$log_dir/run_metadata.txt"

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
finished=1

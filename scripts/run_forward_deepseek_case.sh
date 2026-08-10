#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <workdir> <prompt-file> <plan|resume>" >&2
  exit 2
fi
: "${AIR_API_KEY:?AIR_API_KEY must be set in the calling environment}"

workdir="$1"
prompt_file="$2"
phase="$3"
if [[ ! -f "$prompt_file" ]]; then
  echo "prompt file missing: $prompt_file" >&2
  exit 2
fi

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
      printf 'status=failed\nexit_code=%s\n' "$rc" >> "$log_metadata"
    fi
  fi
  rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup_lock EXIT

export OPENAI_AGENT_API_KEY="$AIR_API_KEY"
export OPENAI_AGENT_BASE_URL="${AIR_API_BASE_URL:-https://api.deepseek.com}"
export AGENT_RUNTIME=openai
export PLANNER_MODEL="${DEEPSEEK_AGENT_MODEL:-deepseek-v4-pro}"
export GENERATOR_MODEL="$PLANNER_MODEL"
export EVALUATOR_MODEL="$PLANNER_MODEL"
export EVALUATOR_MODE=full
export PLAYWRIGHT_HEADLESS=1
export AGENT_MAX_TOOL_CALLS="${FORWARD_MAX_TOOL_CALLS:-48}"
export AGENT_PHASE_TIMEOUT_SECONDS="${FORWARD_AGENT_PHASE_TIMEOUT_SECONDS:-300}"
export AGENT_REQUEST_TIMEOUT_SECONDS="${FORWARD_REQUEST_TIMEOUT_SECONDS:-120}"
export OPENAI_RECENT_MESSAGES="${OPENAI_RECENT_MESSAGES:-10}"
export OPENAI_TOOL_RESULT_CHARS="${OPENAI_TOOL_RESULT_CHARS:-4000}"
export SSL_NO_VERIFY=1
export ALL_PROXY="socks5://127.0.0.1:7897"
export HTTPS_PROXY="socks5://127.0.0.1:7897"
export HTTP_PROXY="socks5://127.0.0.1:7897"
export NO_PROXY="idealab.alibaba-inc.com,alibaba-inc.com,api.deepseek.com,localhost,127.0.0.1"

# DeepSeek V4 is text-only. Do not silently reuse another paid key for visual
# review; a passing text/browser evaluation remains unreleasable until a real
# vision review is supplied.
unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL EVALUATOR_VISION_API_KEY EVALUATOR_VISION_BASE_URL
export EVALUATOR_VISION_MODEL="$PLANNER_MODEL"
export EVALUATOR_VISION_ENDPOINT_TYPE=openai
export EVALUATOR_VISION_TIMEOUT_SECONDS=30
export EVALUATOR_VISION_MAX_RETRIES=0

case_id="$(basename "$workdir")"
run_id="$(date +%Y%m%dT%H%M%S)"
agent_root="$(cd "$(dirname "$0")/.." && pwd)"
data_root="${WEB_CODING_DATA_ROOT:-$(cd "$agent_root/.." && pwd)}"
log_dir="$data_root/logs/agentic/forward_harness_deepseek/$case_id/$run_id"
mkdir -p "$log_dir"
printf 'status=started\nphase=%s\nworkdir=%s\nprompt_file=%s\nmodel=%s\n' \
  "$phase" "$workdir" "$prompt_file" "$PLANNER_MODEL" > "$log_dir/run_metadata.txt"
log_metadata="$log_dir/run_metadata.txt"

prompt="$(<"$prompt_file")"
max_budget="${FORWARD_MAX_BUDGET:-3}"
max_rounds="${FORWARD_MAX_ROUNDS:-4}"
case "$phase" in
  plan)
    uv run python -m src.main "$prompt" --workdir "$workdir" --keep-frontend \
      --plan-only --max-rounds "$max_rounds" --max-budget "$max_budget" --planner-scope-mode query-aligned \
      --playwright-headless 2>&1 | tee "$log_dir/harness.log"
    ;;
  resume)
    uv run python -m src.main "$prompt" --workdir "$workdir" --resume \
      --max-rounds "$max_rounds" --max-budget "$max_budget" --planner-scope-mode query-aligned \
      --playwright-headless 2>&1 | tee "$log_dir/harness.log"
    ;;
  *)
    echo "phase must be plan or resume" >&2
    exit 2
    ;;
esac
finished=1

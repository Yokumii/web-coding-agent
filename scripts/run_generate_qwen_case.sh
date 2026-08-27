#!/usr/bin/env bash
# Run one resumable Generate-led trajectory with strict cost/evidence limits.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <workdir> <prompt-file> <run|resume>" >&2
  exit 2
fi

workdir="$1"
prompt_file="$2"
phase="$3"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
case_id="$(basename "$workdir")"
run_id="$(date +%Y%m%dT%H%M%S)"
log_dir="$repo_root/logs/qwen_generate/$case_id/$run_id"
mkdir -p "$log_dir" "$workdir"
source "$repo_root/scripts/qwen_env.sh"
qwen_proxy_on

export MAX_BUDGET_USD="${GENERATE_MAX_BUDGET:-15}"
export PLANNER_BUDGET_USD="${GENERATE_PLANNER_BUDGET:-1.5}"
export GENERATOR_BUDGET_USD="${GENERATE_GENERATOR_BUDGET:-10}"
export EVALUATOR_BUDGET_USD="${GENERATE_EVALUATOR_BUDGET:-3.5}"
export AGENT_MAX_TOOL_CALLS="${GENERATE_MAX_TOOL_CALLS:-44}"
export AGENT_PHASE_TIMEOUT_SECONDS="${GENERATE_PHASE_TIMEOUT_SECONDS:-600}"
export GENERATE_MAX_ROUNDS="${GENERATE_MAX_ROUNDS:-6}"
export EVALUATOR_MODE="full"
export DESIGN_MODE="text-only"

printf 'status=started\nphase=%s\nworkdir=%s\nprompt_file=%s\nmodel=%s\nmax_budget=%s\n' \
  "$phase" "$workdir" "$prompt_file" "$GENERATOR_MODEL" "$MAX_BUDGET_USD" \
  > "$log_dir/run_metadata.txt"
printf '%q ' uv run python -m src.main "$(<"$prompt_file")" \
  --workdir "$workdir" --task-mode generate --final-project-mode \
  --max-rounds "$GENERATE_MAX_ROUNDS" --max-budget "$MAX_BUDGET_USD" --planner-scope-mode query-aligned \
  --playwright-headless > "$log_dir/command.txt"
printf '\n' >> "$log_dir/command.txt"

resume_flag=""
if [[ "$phase" == "resume" ]]; then
  resume_flag="--resume"
elif [[ "$phase" != "run" ]]; then
  echo "phase must be run or resume" >&2
  exit 2
fi

set +e
uv run python -m src.main "$(<"$prompt_file")" \
  --workdir "$workdir" --task-mode generate --final-project-mode \
  --max-rounds "$GENERATE_MAX_ROUNDS" --max-budget "$MAX_BUDGET_USD" --planner-scope-mode query-aligned \
  --playwright-headless ${resume_flag:+"$resume_flag"} 2>&1 | tee "$log_dir/harness.log"
exit_code=${PIPESTATUS[0]}
set -e
printf 'status=%s\nexit_code=%d\n' \
  "$([[ $exit_code -eq 0 ]] && printf finished || printf error)" "$exit_code" \
  >> "$log_dir/run_metadata.txt"
printf '%s\n' "$log_dir"
exit "$exit_code"

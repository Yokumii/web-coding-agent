#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$(cd "$AGENT_DIR/.." && pwd)"
RUN_ID="20260813_v3"
CASE_FROM="${CASE_FROM:-1}"
CASE_TO="${CASE_TO:-2}"
if [[ ! "$CASE_FROM" =~ ^[12]$ || ! "$CASE_TO" =~ ^[12]$ || "$CASE_FROM" -gt "$CASE_TO" ]]; then
  printf 'CASE_FROM and CASE_TO must define a range within 1..2\n' >&2
  exit 2
fi
OUTPUT_DIR="$DATA_DIR/runs/agentic/minimal_path_calibration/$RUN_ID"
OUTPUT="$OUTPUT_DIR/records.jsonl"
LOG_DIR="$DATA_DIR/logs/minimal_path_calibration/$RUN_ID"
mkdir -p "$OUTPUT_DIR/artifacts" "$LOG_DIR"

exec > >(tee -a "$LOG_DIR/run.log") 2>&1
trap 'status=$?; printf "%s status=%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$status" >> "$LOG_DIR/exit_status.txt"; exit "$status"' EXIT

printf '%s CASE_FROM=%s CASE_TO=%s\n' "scripts/run_minimal_path_calibration.sh" "$CASE_FROM" "$CASE_TO" >> "$LOG_DIR/command.txt"
{
  printf 'started_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'uv=%s\n' "$(uv --version)"
  printf 'node=%s\n' "$(node --version)"
} >> "$LOG_DIR/environment.txt"

cd "$AGENT_DIR"

if [[ "$CASE_FROM" -le 1 && "$CASE_TO" -ge 1 ]]; then
  uv run python scripts/calibrate_minimal_path_cases.py \
    --run-dir "$DATA_DIR/runs/agentic/forward_edit/air_truthchecked_back_to_top_v1_20260807" \
    --round 1 --sprint 1 --mode generate --kind edit \
    --source 9431392 --destination 4837d9a --port 5191 \
    --case-timeout 900 --max-attempts 2 \
    --output-jsonl "$OUTPUT" --artifact-dir "$OUTPUT_DIR/artifacts"
fi

if [[ "$CASE_FROM" -le 2 && "$CASE_TO" -ge 2 ]]; then
  uv run python scripts/calibrate_minimal_path_cases.py \
    --run-dir "$DATA_DIR/runs/agentic/forward_edit/edit_3662_store_tools_v4_20260807" \
    --round 2 --sprint 1 --mode generate --kind edit \
    --source 933ff09 --destination b5f1242 --port 5192 \
    --case-timeout 900 --max-attempts 2 \
    --output-jsonl "$OUTPUT" --artifact-dir "$OUTPUT_DIR/artifacts"
fi

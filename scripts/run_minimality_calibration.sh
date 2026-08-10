#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$(cd "$AGENT_DIR/.." && pwd)"
OUTPUT="$DATA_DIR/runs/agentic/minimality_calibration/20260811_v1/records.jsonl"
LOG_DIR="$DATA_DIR/logs/minimality_calibration/20260811_v1"
mkdir -p "$LOG_DIR"

exec > >(tee -a "$LOG_DIR/run.log") 2>&1
trap 'status=$?; printf "%s\n" "$status" > "$LOG_DIR/exit_status.txt"; exit "$status"' EXIT

printf '%s\n' "scripts/run_minimality_calibration.sh" > "$LOG_DIR/command.txt"
{
  printf 'started_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'uv=%s\n' "$(uv --version)"
  printf 'node=%s\n' "$(node --version)"
} > "$LOG_DIR/environment.txt"

cd "$AGENT_DIR"

uv run python scripts/calibrate_minimality_cases.py \
  --run-dir "$DATA_DIR/runs/agentic/forward_edit/air_truthchecked_back_to_top_v1_20260807" \
  --round 1 \
  --sprint 1 \
  --mode generate \
  --edit-source 9431392 \
  --round-source 9431392 \
  --destination 4837d9a \
  --output-jsonl "$OUTPUT"

uv run python scripts/calibrate_minimality_cases.py \
  --run-dir "$DATA_DIR/runs/agentic/forward_edit/edit_3662_store_tools_v4_20260807" \
  --round 2 \
  --sprint 1 \
  --mode repair \
  --edit-source 933ff09 \
  --round-source b5f1242 \
  --destination 9f6056a \
  --timeout 600 \
  --output-jsonl "$OUTPUT"

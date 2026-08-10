#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 5 ]]; then
  echo "usage: $0 <case-id> <seed-dir> <original-query-file> <evidence-file> <output-jsonl>" >&2
  exit 2
fi
: "${AIR_API_KEY:?AIR_API_KEY must be set in the calling environment}"

case_id="$1"
seed_dir="$2"
original_query_file="$3"
evidence_file="$4"
output_jsonl="$5"

export AIR_API_BASE_URL="${AIR_API_BASE_URL:-https://api.deepseek.com}"
export AIR_MODEL="${AIR_MODEL:-deepseek-chat}"
export AIR_TRUST_ENV=1
export ALL_PROXY="socks5://127.0.0.1:7897"
export HTTPS_PROXY="socks5://127.0.0.1:7897"
export HTTP_PROXY="socks5://127.0.0.1:7897"
export NO_PROXY="idealab.alibaba-inc.com,alibaba-inc.com,api.deepseek.com,localhost,127.0.0.1"
export SSL_NO_VERIFY="${SSL_NO_VERIFY:-1}"

agent_root="$(cd "$(dirname "$0")/.." && pwd)"
data_root="${WEB_CODING_DATA_ROOT:-$(cd "$agent_root/.." && pwd)}"
run_id="$(date +%Y%m%dT%H%M%S)"
log_dir="$data_root/logs/agentic/air_task_refinement/$case_id/$run_id"
mkdir -p "$log_dir"
printf 'status=started\ncase_id=%s\nseed_dir=%s\nmodel=%s\n' \
  "$case_id" "$seed_dir" "$AIR_MODEL" > "$log_dir/run_metadata.txt"

set +e
uv run python scripts/generate_air_webcompass_task.py \
  --mode refine --case-id "$case_id" --seed-dir "$seed_dir" \
  --original-query-file "$original_query_file" --evidence-file "$evidence_file" \
  --output "$output_jsonl" 2>&1 | tee "$log_dir/generation.log"
rc=${PIPESTATUS[0]}
set -e
printf 'status=%s\nexit_code=%s\n' "$([[ $rc -eq 0 ]] && echo finished || echo failed)" "$rc" \
  >> "$log_dir/run_metadata.txt"
exit "$rc"

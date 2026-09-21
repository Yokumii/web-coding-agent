#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${repo_dir}/scripts/qwen_env.sh"

unset ALL_PROXY HTTPS_PROXY HTTP_PROXY all_proxy https_proxy http_proxy
export SSL_NO_VERIFY=1
export OPENAI_ENABLE_THINKING=0
export PYTHONUNBUFFERED=1

run_stamp="$(date +%Y%m%dT%H%M%S)"
launcher_log="${repo_dir}/logs/single_to_multipage_edit/launcher_${run_stamp}.log"
mkdir -p "$(dirname "${launcher_log}")"

cd "${repo_dir}"
echo "run_stamp=${run_stamp} model=qwen3.6-plus retries=0" | tee -a "${launcher_log}"
set +e
uv run python scripts/run_single_to_multipage_edit.py \
  --model qwen3.6-plus "$@" 2>&1 | tee -a "${launcher_log}"
run_status=${PIPESTATUS[0]}
set -e
echo "exit_status=${run_status}" | tee -a "${launcher_log}"
exit "${run_status}"

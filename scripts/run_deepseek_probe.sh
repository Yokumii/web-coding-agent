#!/usr/bin/env bash
set -euo pipefail

: "${AIR_API_KEY:?AIR_API_KEY must be set in the calling environment}"
export AIR_API_BASE_URL="${AIR_API_BASE_URL:-https://api.deepseek.com}"
export AIR_MODEL="${AIR_MODEL:-deepseek-chat}"
export SSL_NO_VERIFY="${SSL_NO_VERIFY:-1}"

agent_root="$(cd "$(dirname "$0")/.." && pwd)"
data_root="${WEB_CODING_DATA_ROOT:-$(cd "$agent_root/.." && pwd)}"
run_id="$(date +%Y%m%dT%H%M%S)"
log_dir="$data_root/logs/agentic/deepseek_probe/$run_id"
mkdir -p "$log_dir"

printf 'status=started\nbase_url=%s\nmodel=%s\n' "$AIR_API_BASE_URL" "$AIR_MODEL" \
  > "$log_dir/run_metadata.txt"

run_probe() {
  local proxy_mode="$1"
  local stream_mode="$2"
  local output="$log_dir/${proxy_mode}_${stream_mode}.json"
  if [[ "$proxy_mode" == "proxy" ]]; then
    export ALL_PROXY="socks5://127.0.0.1:7897"
    export HTTPS_PROXY="socks5://127.0.0.1:7897"
    export HTTP_PROXY="socks5://127.0.0.1:7897"
    export NO_PROXY="idealab.alibaba-inc.com,alibaba-inc.com,api.deepseek.com,localhost,127.0.0.1"
    trust_env="on"
  else
    unset ALL_PROXY HTTPS_PROXY HTTP_PROXY
    export NO_PROXY="idealab.alibaba-inc.com,alibaba-inc.com,api.deepseek.com,localhost,127.0.0.1"
    trust_env="off"
  fi
  if [[ "$stream_mode" == "stream" ]]; then
    uv run python scripts/probe_openai_compat_api.py \
      --trust-env "$trust_env" --stream 2>&1 | tee "$output"
  else
    uv run python scripts/probe_openai_compat_api.py \
      --trust-env "$trust_env" 2>&1 | tee "$output"
  fi
}

run_probe direct nonstream
run_probe direct stream
run_probe proxy nonstream
run_probe proxy stream

printf 'status=finished\n' >> "$log_dir/run_metadata.txt"
printf '%s\n' "$log_dir"

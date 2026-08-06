#!/usr/bin/env bash
# Source from web-coding-agent/. Credentials stay only in this shell's environment.
set -euo pipefail

export OPENAI_AGENT_API_KEY="$(pdftotext ../docs/项目用api.pdf - | awk 'match($0,/sk-[A-Za-z0-9]+/){print substr($0,RSTART,RLENGTH); exit}')"
export OPENAI_AGENT_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export AGENT_RUNTIME="openai"
export OPENAI_ENABLE_THINKING="0"
export PLANNER_MODEL="qwen3.6-plus"
export GENERATOR_MODEL="qwen3.6-plus"
export EVALUATOR_MODEL="qwen3.6-plus"
export EVALUATOR_VISION_MODEL="qwen3.6-plus"
export EVALUATOR_VISION_API_KEY="$OPENAI_AGENT_API_KEY"
export EVALUATOR_VISION_BASE_URL="$OPENAI_AGENT_BASE_URL"
export EVALUATOR_VISION_ENDPOINT_TYPE="openai"
export PLAYWRIGHT_HEADLESS="1"
export AGENT_PHASE_TIMEOUT_SECONDS="900"
export AGENT_REQUEST_TIMEOUT_SECONDS="180"
export AGENT_MAX_TOOL_CALLS="100"
export SSL_NO_VERIFY="1"

qwen_proxy_on() {
  export QWEN_PROXY_PORT="${QWEN_PROXY_PORT:-7897}"
  export ALL_PROXY="socks5://127.0.0.1:${QWEN_PROXY_PORT}"
  export HTTPS_PROXY="socks5://127.0.0.1:${QWEN_PROXY_PORT}"
  export HTTP_PROXY="socks5://127.0.0.1:${QWEN_PROXY_PORT}"
  export NO_PROXY="idealab.alibaba-inc.com,alibaba-inc.com,api.deepseek.com,localhost,127.0.0.1"
}

qwen_proxy_off() {
  unset ALL_PROXY HTTPS_PROXY HTTP_PROXY
}

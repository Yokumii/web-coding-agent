#!/usr/bin/env bash
# Source from web-coding-agent/. Credentials stay only in this shell's environment.
set -euo pipefail

qwen_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
qwen_api_pdf="${QWEN_API_PDF:-$qwen_repo_root/../WebCoding_Data/docs/项目用api.pdf}"
if [[ ! -f "$qwen_api_pdf" ]]; then
  echo "Qwen API credential PDF not found; set QWEN_API_PDF" >&2
  return 1 2>/dev/null || exit 1
fi
# Consume the entire converter stream. With `pipefail`, exiting awk after the
# first match can SIGPIPE pdftotext and abort the sourced script nondeterministically.
export OPENAI_AGENT_API_KEY="$(pdftotext "$qwen_api_pdf" - | awk 'match($0,/sk-[A-Za-z0-9]+/){if (!found) {print substr($0,RSTART,RLENGTH); found=1}}')"
if [[ -z "$OPENAI_AGENT_API_KEY" ]]; then
  echo "Qwen API credential could not be extracted" >&2
  return 1 2>/dev/null || exit 1
fi
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
# Vision review is a quality gate, not an unbounded batch worker.  One stalled
# request must become an explicit infrastructure failure instead of consuming
# the remainder of a calibration case.
export EVALUATOR_VISION_TIMEOUT_SECONDS="${EVALUATOR_VISION_TIMEOUT_SECONDS:-120}"
export EVALUATOR_VISION_MAX_RETRIES="${EVALUATOR_VISION_MAX_RETRIES:-1}"
export PLAYWRIGHT_HEADLESS="1"
# Calibration cases must fail explicitly rather than leaving an unavailable API
# request alive for fifteen minutes. Callers may raise these limits for a
# deliberate long-running final batch.
export AGENT_PHASE_TIMEOUT_SECONDS="${AGENT_PHASE_TIMEOUT_SECONDS:-300}"
export AGENT_REQUEST_TIMEOUT_SECONDS="${AGENT_REQUEST_TIMEOUT_SECONDS:-120}"
# Qwen charges every retained tool turn as input context.  Keep a focused
# rolling context for dataset production. A real multi-file calibration showed
# that 10 messages / 4K tool output caused repeated source reads and cost more
# than retaining enough local context to validate and commit in one attempt.
export OPENAI_RECENT_MESSAGES="${OPENAI_RECENT_MESSAGES:-20}"
export OPENAI_TOOL_RESULT_CHARS="${OPENAI_TOOL_RESULT_CHARS:-8000}"
export AGENT_MAX_TOOL_CALLS="${AGENT_MAX_TOOL_CALLS:-48}"
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

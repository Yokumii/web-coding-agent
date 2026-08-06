#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <proxy:on|off> <stream:on|off>" >&2
  exit 2
fi
source "$(dirname "$0")/qwen_env.sh"
if [[ "$1" == "on" ]]; then qwen_proxy_on; elif [[ "$1" == "off" ]]; then qwen_proxy_off; else exit 2; fi
if [[ "$2" == "on" ]]; then
  uv run python scripts/probe_qwen_api.py --stream
elif [[ "$2" == "off" ]]; then
  uv run python scripts/probe_qwen_api.py
else
  exit 2
fi

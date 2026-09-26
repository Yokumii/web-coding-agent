#!/bin/sh
set -eu

export DOCKER_HOST="${DOCKER_HOST:-unix:///var/run/docker.sock}"
export HARNESS_DATA_ROOT="${HARNESS_DATA_ROOT:-/data2/adminweihunj/webcoding/WebCoding_Data}"
export NJULINK_KEY_FILE="${NJULINK_KEY_FILE:-/home/adminweihunj/.config/webcoding/credentials/njulink-luna.key}"
export HTTP_PROXY="${HTTP_PROXY:-http://127.0.0.1:7890}"
export HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:7890}"
export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1}"
export HARNESS_UID="${HARNESS_UID:-$(id -u)}"
export HARNESS_GID="${HARNESS_GID:-$(id -g)}"

docker build --network host \
  --build-arg HTTP_PROXY="$HTTP_PROXY" \
  --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  --build-arg NO_PROXY="$NO_PROXY" \
  -t webcoding-edit-harness:20260926 -f Dockerfile ..
docker compose run --rm harness sh -lec '
  .venv/bin/python -c "import playwright, src; print(\"python-runtime-ok\")"
  node -e "const {chromium}=require(\"/opt/reverse-validator/node_modules/playwright\"); (async()=>{const browser=await chromium.launch({headless:true}); await browser.close(); console.log(\"browser-runtime-ok\")})().catch(error=>{console.error(error);process.exit(1)})"
  test -f /app/reverse/validate/validate.js
  test -f /app/reverse/validate/build_vite_project.mjs
'

# Makefile — convenience wrappers around the Docker harness.
# All targets ultimately call `docker` or `docker compose`.
#
# Variables:
#   IMAGE    image tag (default: web-coding-agent:latest)
#   WORKDIR  host path bind-mounted to /app/workdir (default: ./workdir)
#   PROMPT   harness prompt (required for `run` / `plan-only`)
#
# Examples:
#   make build
#   make test
#   make plan-only PROMPT="Build a counter app"
#   make run PROMPT="Build a counter app" WORKDIR=./e2e-out
#   make shell
#   make clean

IMAGE   ?= web-coding-agent:latest
WORKDIR ?= $(CURDIR)/workdir

.PHONY: help build test plan-only run shell clean

help:
	@echo "Targets:"
	@echo "  build       Build the Docker image ($(IMAGE))"
	@echo "  test        Run pytest inside the container"
	@echo "  plan-only   Run harness --plan-only (requires PROMPT=...)"
	@echo "  run         Run full harness (requires PROMPT=...)"
	@echo "  shell       Open a shell inside the image"
	@echo "  clean       Remove the built image"
	@echo ""
	@echo "Variables (override on the make command line):"
	@echo "  IMAGE=$(IMAGE)"
	@echo "  WORKDIR=$(WORKDIR)"
	@echo "  PROMPT=<harness prompt — required for plan-only / run>"

build:
	docker build -t $(IMAGE) .

test: build
	docker run --rm \
	  --entrypoint uv \
	  $(IMAGE) \
	  run pytest tests -q

# Ensure the host workdir exists with caller's UID before docker mounts it.
$(WORKDIR):
	mkdir -p $(WORKDIR)

plan-only: build $(WORKDIR)
	@if [ -z "$(PROMPT)" ]; then \
	  echo "ERROR: PROMPT is required, e.g. make plan-only PROMPT=\"Build a counter app\""; \
	  exit 2; \
	fi
	HARNESS_WORKDIR=$(WORKDIR) docker compose run --rm harness "$(PROMPT)" --workdir /app/workdir --plan-only

run: build $(WORKDIR)
	@if [ -z "$(PROMPT)" ]; then \
	  echo "ERROR: PROMPT is required, e.g. make run PROMPT=\"Build a counter app\""; \
	  exit 2; \
	fi
	HARNESS_WORKDIR=$(WORKDIR) docker compose run --rm harness "$(PROMPT)" --workdir /app/workdir --playwright-headless

# `shell` deliberately bypasses compose: debugging often needs a
# writable rootfs (ad-hoc apt-get / pip install). Env vars are NOT
# forwarded — set them inline with `-e VAR=...`, or use
# `docker compose run --rm --entrypoint /bin/bash harness` for the
# isolated variant with .env forwarding.
shell: build
	docker run --rm -it \
	  --entrypoint /bin/bash \
	  -v $(WORKDIR):/app/workdir:rw \
	  $(IMAGE)

clean:
	-docker rmi $(IMAGE)

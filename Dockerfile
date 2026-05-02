# syntax=docker/dockerfile:1.7

# Microsoft Playwright image bundles Chromium + system libs + Node 20.
# Pinned to keep Chromium / system-libs reproducible between hosts.
FROM mcr.microsoft.com/playwright:v1.49.1-jammy

# Explicit root for the apt layer; the base image's default user is
# undocumented and could change between tags.
USER root

# Jammy's default Python is 3.10; the project requires >=3.11.
# python3.11-venv is required because uv creates ephemeral venvs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (pwuser, UID 1000) is provided by the base image.
# Switch before installing uv so the binary lands in $HOME/.local/bin.
USER pwuser
ENV HOME=/home/pwuser
ENV PATH="${HOME}/.local/bin:${PATH}"

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app

# Copy lockfiles + Python pin first so the dependency layer caches across
# src/ edits. .python-version pins uv to /usr/bin/python3.11.
COPY --chown=pwuser:pwuser pyproject.toml uv.lock .python-version ./

# Keep the dev group so pytest is available inside the container.
RUN uv sync --frozen

COPY --chown=pwuser:pwuser src/ ./src/
COPY --chown=pwuser:pwuser tests/ ./tests/

# Chromium's setuid sandbox can't run as a non-root user in an
# unprivileged container, and the MCP CLI doesn't always forward
# --no-sandbox. See playwright/playwright issue #883.
ENV PLAYWRIGHT_MCP_SANDBOX=false

ENV PYTHONUNBUFFERED=1

# The read-only root FS in compose rejects ~/.cache/uv writes at startup
# ("Could not acquire lock"). Redirect uv's cache to /tmp (tmpfs).
ENV UV_CACHE_DIR=/tmp/uv-cache

# Invoke the harness via `python -m src.main` because pyproject.toml
# lacks a [build-system] section, so `uv sync` does not install the
# project as a package and the `harness` console script is never
# created. CLI args appended to `docker run` flow straight in.
ENTRYPOINT ["uv", "run", "python", "-m", "src.main"]
CMD ["--help"]

# syntax=docker/dockerfile:1.7

FROM node:20-bookworm-slim AS node-runtime

# Match the Python Playwright version pinned by uv.lock. The image provides
# Chromium and its Linux system dependencies for the physical x86_64 host.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

# Explicit root for the apt layer; the base image's default user is
# undocumented and could change between tags.
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git lsof \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/ /usr/local/lib/node_modules/
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ENV UV_INSTALL_DIR=/usr/local/bin
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app/harness
COPY --chown=pwuser:pwuser harness/pyproject.toml harness/uv.lock harness/.python-version ./

RUN uv sync --frozen
RUN chmod -R a+rX /opt/uv-python /app/harness/.venv

COPY reverse/validate/package.json reverse/validate/package-lock.json /opt/reverse-validator/
RUN npm ci --prefix /opt/reverse-validator --omit=dev --no-audit --no-fund

COPY --chown=pwuser:pwuser harness/.agents/ ./.agents/
COPY --chown=pwuser:pwuser harness/config/ ./config/
COPY --chown=pwuser:pwuser harness/src/ ./src/
COPY --chown=pwuser:pwuser harness/tests/ ./tests/
COPY --chown=pwuser:pwuser harness/scripts/ ./scripts/
COPY --chown=pwuser:pwuser harness/docker/ ./docker/
COPY --chown=pwuser:pwuser reverse/validate/ /app/reverse/validate/

RUN chmod 0755 /app/harness/docker/entrypoint.sh \
    && mkdir -p /app/workdir \
    && chown pwuser:pwuser /app/workdir

# Chromium's setuid sandbox can't run as a non-root user in an
# unprivileged container, and the MCP CLI doesn't always forward
# --no-sandbox. See playwright/playwright issue #883.
ENV PLAYWRIGHT_MCP_SANDBOX=false
ENV PYTHONUNBUFFERED=1
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV NODE_PATH=/opt/reverse-validator/node_modules

USER root
ENTRYPOINT ["/app/harness/docker/entrypoint.sh"]
CMD ["uv", "run", "python", "-m", "src.main", "--help"]

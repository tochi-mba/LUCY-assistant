# syntax=docker/dockerfile:1

# A plain slim base. The hub makes outbound HTTPS calls to the family and to a model
# provider, and holds session state in SQLite. It runs no browser and executes no untrusted
# code -- that is environments-api's job, behind its own sandbox.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first, so editing the hub does not invalidate it.
COPY pyproject.toml uv.lock README.md ./
# git: uv fetches the family's client packages from tagged git sources.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# The token exists only for this RUN, in git's process environment, never a layer.
# Without a secret, public sources are fetched anonymously.
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-install-project --no-dev

COPY src/ src/
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-dev

# Session state, workspaces metadata and the result store are written at runtime and must
# not live in an image layer. 0700 because the contents are a person's conversation.
RUN mkdir -p /var/lib/lucy /var/log/lucy \
    && useradd --create-home --uid 10001 lucy \
    && chown -R lucy:lucy /var/lib/lucy /var/log/lucy /app \
    && chmod 0700 /var/lib/lucy
USER lucy

ENV LUCY_HOST=0.0.0.0 \
    LUCY_PORT=8000 \
    LUCY_LOG_FORMAT=json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthy', timeout=4).status == 200 else 1)"

CMD ["lucy-api"]

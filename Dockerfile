# syntax=docker/dockerfile:1

# The native Vonage Video Connector ships Linux wheels (manylinux_2_35, x86_64 and
# aarch64), so the Pipecat agent always runs in this Linux image, on any host OS.
# Debian bookworm provides glibc 2.36.
FROM python:3.12-slim-bookworm AS base

# OS libraries the vonage-video-connector wheel loads at import time
# (same list as Vonage's official Video Connector server samples).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1


FROM base AS builder

RUN pip install --no-cache-dir uv==0.11.32

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
COPY pyproject.toml uv.lock ./
# --frozen: install exactly what uv.lock says; fail instead of re-resolving.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# Optional target that runs the automated tests inside Linux:
#   docker build --target test -t vonage-pipecat-openai-realtime:test .
FROM builder AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen
COPY app ./app
COPY web ./web
COPY tests ./tests
ENV PATH="/opt/venv/bin:$PATH"
RUN ruff check app tests && mypy && pytest -q


FROM base AS runtime

# Run as an unprivileged user. The Vonage private key is mounted read-only at
# runtime (see compose.yaml) and is never copied into the image.
RUN useradd --create-home --uid 10001 app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
COPY web ./web

ENV PATH="/opt/venv/bin:$PATH"

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]

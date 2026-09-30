# syntax=docker/dockerfile:1

FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
	UV_LINK_MODE=copy \
	UV_PYTHON_DOWNLOADS=0
WORKDIR /srv

# Dependencies first so this layer is cached across code changes.
RUN --mount=type=cache,target=/root/.cache/uv \
	--mount=type=bind,source=uv.lock,target=uv.lock \
	--mount=type=bind,source=pyproject.toml,target=pyproject.toml \
	uv sync --locked --no-dev --no-install-project

COPY . /srv
RUN --mount=type=cache,target=/root/.cache/uv \
	uv sync --locked --no-dev


FROM python:3.14-slim
# WeasyPrint's native libraries, plus fonts: the slim image has none, so PDFs would have no text.
# Liberation fonts are metric-compatible with Arial, Times New Roman and Courier New.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core fonts-liberation \
	&& fc-cache -f \
	&& rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/
RUN groupadd --system app && useradd --system --gid app --no-create-home app
# Code stays root-owned (read-only to the app); only the local file store is writable.
COPY --from=builder /srv /srv
RUN mkdir -p /srv/.data && chown app:app /srv/.data
WORKDIR /srv

# UV_NO_SYNC makes `uv run ...` use the venv built above as-is: no dependency install or
# lockfile check at startup, so either `python main.py` or `uv run python main.py` works.
ENV PATH="/srv/.venv/bin:$PATH" \
	PYTHONUNBUFFERED=1 \
	PYTHONDONTWRITEBYTECODE=1 \
	UV_NO_SYNC=1 \
	UV_PYTHON_DOWNLOADS=0 \
	UV_CACHE_DIR=/tmp/uv-cache \
	XDG_CACHE_HOME=/tmp/cache \
	APP_ENVIRONMENT=production \
	APP_SERVER__HOST=0.0.0.0 \
	APP_SERVER__PORT=8000

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
	CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['APP_SERVER__PORT']}/health/live\", timeout=2)"]

CMD ["python", "main.py"]

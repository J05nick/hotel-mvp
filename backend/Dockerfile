# syntax=docker/dockerfile:1.7
# =============================================================================
# Backend image — shared by api, celery-worker, celery-beat and migrate.
# Build targets:
#   dev  -> includes dev dependency group (pytest, mypy, ruff); used by compose
#   prod -> runtime deps only, non-root, no source bind-mounts
# =============================================================================
ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/
RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app

# ---------------------------------------------------------------- prod deps
FROM base AS deps-prod
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ---------------------------------------------------------------- dev deps
FROM base AS deps-dev
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen

# ---------------------------------------------------------------- dev
FROM deps-dev AS dev
COPY --chown=app:app . .
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host=0.0.0.0", "--port=8000", "--reload"]

# ---------------------------------------------------------------- prod
FROM deps-prod AS prod
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host=0.0.0.0", "--port=8000", "--proxy-headers", "--no-access-log"]

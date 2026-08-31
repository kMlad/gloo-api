# Multi-stage image: uv installs into a venv in the builder, then the
# runtime image runs without uv. Builder and runtime Python/OS must match
# so the venv interpreter path stays valid.
# See https://docs.astral.sh/uv/guides/integration/docker/

FROM ghcr.io/astral-sh/uv:0.12.7-python3.13-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

FROM python:3.13-slim-trixie

RUN groupadd --system --gid 999 nonroot \
    && useradd --system --gid 999 --uid 999 --create-home nonroot

COPY --from=builder --chown=nonroot:nonroot /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER nonroot
WORKDIR /app

EXPOSE 8000

CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000", "app/main.py"]

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/tmp/uv-cache \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    FRAMEKEEP_HOST=0.0.0.0 \
    FRAMEKEEP_PORT=4173 \
    FRAMEKEEP_DATA_DIR=/var/lib/framekeep \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-noto-cjk \
    && uv sync --frozen --no-dev --no-install-project \
    && rm -rf /tmp/uv-cache \
    && uv run --frozen --no-sync python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* /tmp/uv-cache \
    && groupadd --gid 10001 framekeep \
    && useradd --create-home --uid 10001 --gid 10001 framekeep \
    && mkdir -p /var/lib/framekeep \
    && chown framekeep:framekeep /var/lib/framekeep \
    && chmod 700 /var/lib/framekeep \
    && chmod -R a+rX /ms-playwright

COPY backend ./backend
COPY dist ./dist

USER framekeep
EXPOSE 4173
CMD ["uv", "run", "--frozen", "--no-sync", "python", "-m", "backend"]

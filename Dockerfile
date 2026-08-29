FROM ghcr.io/astral-sh/uv:0.8.14 AS uv
FROM python:3.12.11-slim-bookworm

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nginx supervisor \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 iwiki \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin iwiki

WORKDIR /app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY deploy ./deploy
RUN uv sync --frozen --no-dev --no-editable \
    && chown -R 10001:10001 /app

COPY deploy/supervisord.conf /etc/supervisor/supervisord.conf

USER 10001:10001
HEALTHCHECK NONE
ENTRYPOINT ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron openssh-client rsync \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY configs ./configs
COPY src ./src
COPY deploy/docker/crontab /etc/cron.d/quant-cron

RUN chmod 0644 /etc/cron.d/quant-cron \
    && crontab /etc/cron.d/quant-cron \
    && mkdir -p /app/data /app/snapshots /app/reports /app/logs

ENV PATH="/app/.venv/bin:${PATH}"
CMD ["cron", "-f"]

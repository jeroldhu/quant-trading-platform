FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron openssh-client rsync \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY configs ./configs
COPY src ./src
# 依赖已在上一层缓存；安装项目本身的可编辑引用
RUN uv pip install -e . --no-deps
# crontab 放入 /etc/cron.d/ 即可被 cron 自动加载，不通过 crontab 命令导入
# （/etc/cron.d/ 格式含第 6 字段用户，作为用户 crontab 导入会破坏命令）
COPY deploy/docker/crontab /etc/cron.d/quant-cron

RUN chmod 0644 /etc/cron.d/quant-cron \
    && mkdir -p /app/data /app/snapshots /app/reports /app/logs

ENV PATH="/app/.venv/bin:${PATH}"
CMD ["cron", "-f"]

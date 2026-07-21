# 运维手册

> 版本: v1.0.0 | 日期: 2026-07-21

本文档覆盖 Quant Trading 系统的部署、调度、备份、监控和故障恢复。

---

## 1. 部署架构

```
┌─────────────────────────────────────────────────┐
│                 阿里云 ECS                        │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ scheduler │  │postgresql│  │   snapshots/   │  │
│  │ (Docker)  │  │ (Docker) │  │   (Bind Mount) │  │
│  └─────┬─────┘  └────┬─────┘  └───────┬───────┘  │
│        │              │               │           │
│        ▼              ▼               ▼           │
│  /srv/quant-trading/data/      /srv/quant-trading │
│  /srv/quant-trading/logs/      /snapshots/        │
└─────────────────────────────────────────────────┘
          │                              │
          │ SSH (22)                     │ rsync over SSH
          ▼                              ▼
┌─────────────────────┐    ┌─────────────────────┐
│   研究机 A (macOS)   │    │   研究机 B (macOS)   │
│   quant data pull   │    │   quant data pull   │
│   quant research    │    │   quant research    │
└─────────────────────┘    └─────────────────────┘
```

**核心约束：**
- 阿里云是**唯一写入端**。所有数据采集、校验、发布在服务器上完成
- 研究机只通过 `quant data snapshot pull` 拉取只读快照
- 不得从研究机反向同步数据到服务器
- 不得在服务器上运行 `quant research` 命令

---

## 2. 服务器部署

### 2.1 首次部署

```bash
# 1. 克隆代码到 /root/workspace/quant-trading
cd /root/workspace
git clone <repo-url> quant-trading
cd quant-trading

# 2. 配置环境
cp deploy/aliyun.env.example .env
# 编辑 .env，填入实际值

# 3. 创建持久化目录
mkdir -p /srv/quant-trading/data
mkdir -p /srv/quant-trading/snapshots
mkdir -p /srv/quant-trading/postgresql
mkdir -p /srv/quant-trading/logs

# 4. 构建并启动服务
docker compose build
docker compose --profile server up -d postgresql
docker compose --profile server up -d scheduler

# 5. 初始化数据库和数据
docker compose --profile cli run --rm cli data bootstrap
docker compose --profile cli run --rm cli research init-db

# 6. 回填历史数据（耗时较长，建议在 screen/tmux 中运行）
screen -S backfill
docker compose --profile cli run --rm cli data backfill \
  --start 2024-01-01 --end 2026-07-21
# Ctrl+A, D 分离

# 7. 验证
docker compose --profile cli run --rm cli data status
```

### 2.2 目录权限

```bash
# 创建专用用户，容器以非 root 运行
groupadd -g 1000 quant && useradd -u 1000 -g quant -m quant
chown -R quant:quant /srv/quant-trading/
chmod 755 /srv/quant-trading/
chmod 755 /srv/quant-trading/data/
chmod 755 /srv/quant-trading/snapshots/
```

### 2.3 SSH 配置（供研究机拉取快照）

```bash
# 服务器 /root/.ssh/authorized_keys
# 添加研究机的公钥，只允许 rsync
command="rsync --server --sender -logDtpre.iLsfxC --numeric-ids . /srv/quant-trading/snapshots/",no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding ssh-rsa AAAAB3... researcher@machineA
```

### 2.4 Docker Compose 配置

```yaml
# deploy/docker/docker-compose.yml
services:
  scheduler:
    image: quant-trading:0.1.0                # 固定版本，不用 :latest
    container_name: quant-scheduler
    user: "1000:1000"                          # 非 root
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "uv", "run", "quant", "data", "status"]
      interval: 5m
      timeout: 30s
      retries: 3
      start_period: 60s
    volumes:
      - /srv/quant-trading/data:/app/data
      - /srv/quant-trading/snapshots:/app/snapshots
      - /srv/quant-trading/logs:/app/logs
      - /etc/localtime:/etc/localtime:ro
    env_file:
      - /srv/quant-trading/.env                # 完整注入环境变量
    environment:
      - QUANT_MARKET_MODE=live
      - TZ=Asia/Shanghai
    depends_on:
      postgresql:
        condition: service_healthy

  postgresql:
    image: postgres:16.3-alpine                # 固定补丁版本
    container_name: quant-postgres
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U quant"]
      interval: 10s
      timeout: 5s
      retries: 5
    env_file:
      - /srv/quant-trading/.env
    volumes:
      - /srv/quant-trading/postgresql:/var/lib/postgresql/data
      - ./postgresql/init:/docker-entrypoint-initdb.d
      - ./postgresql/config/postgresql.conf:/etc/postgresql/postgresql.conf
    ports:
      - "127.0.0.1:5432:5432"

  cli:
    image: quant-trading:0.1.0
    profiles: [cli]
    user: "1000:1000"
    entrypoint: ["uv", "run", "quant"]
    volumes:
      - /srv/quant-trading/data:/app/data
      - /srv/quant-trading/snapshots:/app/snapshots
    env_file:
      - /srv/quant-trading/.env
```

### 2.5 Dockerfile

```dockerfile
# deploy/docker/Dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cron curl tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 安装依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --extra market --frozen

# 复制源码
COPY src/ src/
COPY configs/ configs/

# 安装 cron
COPY deploy/docker/crontab /etc/cron.d/quant-cron
RUN chmod 0644 /etc/cron.d/quant-cron && crontab /etc/cron.d/quant-cron

# 入口：启动 cron 前台运行
CMD ["cron", "-f"]
```

---

## 3. 定时调度

### 3.1 调度表

```cron
# /etc/cron.d/quant-cron (容器内)
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
TZ=Asia/Shanghai

# 工作日盘中快照（14:30）
30 14 * * 1-5 root uv run quant data daily --trade-date today --skip-nav 2>&1 | tee -a /app/logs/cron-daily.log

# 收盘第一次采集（15:20，交易所数据已有）
20 15 * * 1-5 root uv run quant data daily --trade-date today --skip-nav 2>&1 | tee -a /app/logs/cron-daily.log

# 收盘补充采集（18:30，含净值）
30 18 * * 1-5 root uv run quant data daily --trade-date today 2>&1 | tee -a /app/logs/cron-daily.log

# 盘后补齐历史（19:30）
30 19 * * 1-5 root uv run quant data backfill --start today --end today 2>&1 | tee -a /app/logs/cron-backfill.log

# 晚间复核（22:30）
30 22 * * 1-5 root uv run quant data reconcile --trade-date today 2>&1 | tee -a /app/logs/cron-reconcile.log

# 次晨复核（07:30）
30  7 * * 1-5 root uv run quant data reconcile --trade-date today 2>&1 | tee -a /app/logs/cron-reconcile.log

# 正式发布（08:00，门禁全通过才发布）
 0  8 * * 1-5 root uv run quant data publish --trade-date latest 2>&1 | tee -a /app/logs/cron-publish.log

# 周末 dev 快照（周五 23:00）
 0 23 * * 5   root uv run quant data snapshot --profile dev 2>&1 | tee -a /app/logs/cron-snapshot.log

# 周日 full 快照（周日 23:30）
30 23 * * 0   root uv run quant data snapshot --profile full 2>&1 | tee -a /app/logs/cron-snapshot.log

# 月初分区合并（每月 1 日 02:00）
 0  2 1  * *  root uv run quant data compact 2>&1 | tee -a /app/logs/cron-compact.log

# 日志清理（每天 03:00，保留 30 天）
 0  3 * * *   root find /app/logs -name "*.log" -mtime +30 -delete
```

### 3.2 调度管理命令

```bash
# 启动调度
quant scheduler start

# 停止调度
quant scheduler stop

# 查看调度状态
quant scheduler status

# 查看最近日志
quant scheduler logs --lines 50
```

### 3.3 任务互斥

cron 任务可能重叠执行（如前一次采集未结束，下一轮 cron 已触发）。
所有数据写入命令使用全局文件锁（`data/.writer.lock`）确保串行执行。

```python
# 每个 data 命令自动获取全局写锁
with global_writer_lock(timeout_seconds=300):
    pipeline.run()
```

如果上一任务超时未释放锁，下一任务等待 5 分钟后报 `LOCK_TIMEOUT` 并退出。

### 3.4 任务幂等性

| 命令 | 幂等键 | 行为 |
|------|--------|------|
| `data daily` | `(trade_date, run_id)` | 同一交易日多次运行：最近一次覆盖 Bronze/Silver，Gold 追加新版本 |
| `data backfill` | `(instrument_id, trade_date, adjustment, source_id)` | 重跑覆盖同主键，不产生重复行 |
| `data publish` | `(trade_date, data_version)` | 同一交易日多次发布：产生多个 Gold 版本 |
| `data snapshot` | `snapshot_id` | 不可重复创建相同 snapshot_id |

### 3.5 退出码

| 退出码 | 含义 | cron 动作 |
|--------|------|-----------|
| 0 | SUCCESS | — |
| 1 | FAILED | 告警 |
| 77 | SKIPPED（非交易日） | 不告警，计入统计 |
| 124 | TIMEOUT | 告警 |
| 125 | LOCK_TIMEOUT | 不告警（上一任务仍在运行） |

### 3.6 跳过非交易日

定时任务不知道当天是否为交易日。`data daily` 命令在无行情数据时返回失败，
外层 `|| true` 可防止 cron 报错但不利于监控。

推荐做法：在 `data daily` 命令内部检查交易日历，非交易日时返回 exit code 77 (SKIP)，
cron 不将其视为失败。

---

## 4. 快照管理

### 4.1 服务器创建快照

```bash
# 开发快照（不含 Raw 响应，研究端日常使用）
quant data snapshot --profile dev

# 完整快照（含 Raw 审计数据，周末/排查问题时使用）
quant data snapshot --profile full

# 保留最近 5 个快照，旧的自动清理
quant data snapshot --profile dev --retain 5
```

### 4.2 研究机拉取快照

```bash
# 首次拉取（全量传输，耗时取决于数据量）
quant data snapshot pull --remote aliyun --profile dev

# 后续拉取（rsync 增量，通常几秒到几分钟）
quant data snapshot pull --remote aliyun --profile dev

# 校验已拉取的快照
quant data snapshot verify

# 拉取完整快照（排查原始数据问题）
quant data snapshot pull --remote aliyun --profile full

# 指定快照 ID（不回退到最新，用于回放特定时点）
quant data snapshot pull --remote aliyun --profile dev \
  --snapshot 20260721T032309084199Z-a1b2c3d4
```

### 4.3 快照恢复保护

拉取快照前，脚本会检查本地 `data/` 目录状态：

| 本地状态 | 默认行为 | 选项 |
|----------|----------|------|
| 为空 | 直接恢复 | — |
| 来自上一快照且无本地修改 | 增量更新 | — |
| 包含本地写入 | 拒绝覆盖 | `--backup-existing` 强制覆盖（旧目录重命名备份） |
| 无法验证来源 | 拒绝覆盖 | 同上 |

```bash
# 强制覆盖前先备份
quant data snapshot pull --remote aliyun --profile dev --backup-existing
# 旧目录备份为 data.backup.20260721T153000/
```

### 4.4 快照生命周期

```
dev 快照：每天发布 → 保留最近 7 个 → 第 8 个自动删除
full 快照：周日发布 → 保留最近 4 个 → 第 5 个自动删除
```

可通过 `--retain` 覆盖：
```bash
quant data snapshot --profile dev --retain 14
```

### 4.5 快照安全

**manifest 完整性：**
- manifest 包含 `data_version`、`schema_version`、`creator_version`（quant CLI 版本）
- manifest 文件本身生成 SHA-256 校验和，存储在 `manifest.sha256`
- latest 指针为符号链接，原子切换：创建新链接 → `mv -T` → 旧快照保留

**latest 切换前检查：**
1. 全部文件存在且 SHA-256 匹配 manifest
2. DuckDB 可打开且视图可查询
3. 门禁均为 READY（或显式标记跳过）
4. 以上任一失败：不创建快照，不更新 latest，错误输出到日志

**SSH 安全：**
```bash
# 服务器 authorized_keys 强制命令
command="rsync --server --sender -logDtpre.iLsfxC --numeric-ids . /srv/quant-trading/snapshots/",\
no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding \
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... researcher@machineA
```
- 使用 Ed25519 密钥（不推荐 RSA）
- 只读 rsync，不能写入
- 禁止端口转发、PTY、X11

**失败恢复：**
- 拉取中断：重新执行 `pull`，rsync 增量续传
- SHA-256 不匹配：清除损坏文件 → 重新 `pull`
- 快照不兼容（schema_version 过低）：升级 CLI 版本或指定旧快照 ID
- 恢复后 DuckDB 视图绑定失败：`quant data snapshot verify --fix-views`

---

## 5. PostgreSQL 运维

### 5.1 连接

PostgreSQL 只监听 `127.0.0.1:5432`，不暴露到公网。

```bash
# 服务器本地连接
docker exec -it quant-postgres psql -U quant

# 研究机通过 SSH 隧道连接
ssh -N -L 15432:127.0.0.1:5432 root@<aliyun-ip>
# 然后在本机连接 127.0.0.1:15432
psql -h 127.0.0.1 -p 15432 -U quant
```

### 5.2 备份

```bash
# 全量备份
docker exec quant-postgres pg_dump -U quant -Fc quant > /srv/quant-trading/backups/quant_$(date +%Y%m%d).dump

# 仅备份周频信号表
docker exec quant-postgres pg_dump -U quant -t weekly_rotation_score -t signal_daily quant > /srv/quant-trading/backups/signals_$(date +%Y%m%d).sql

# 定时备份（添加到 crontab）
0 4 * * * docker exec quant-postgres pg_dump -U quant -Fc quant > /srv/quant-trading/backups/quant_$(date +\%Y\%m\%d).dump 2>&1
0 4 * * * find /srv/quant-trading/backups -name "*.dump" -mtime +30 -delete
```

### 5.3 恢复

```bash
# 全量恢复
docker exec -i quant-postgres pg_restore -U quant -d quant --clean \
  < /srv/quant-trading/backups/quant_20260721.dump

# 恢复后验证
docker exec -i quant-postgres psql -U quant -c \
  "SELECT count(*) FROM weekly_rotation_score"
docker exec -i quant-postgres psql -U quant -c \
  "SELECT max(signal_date) FROM signal_daily"

# 恢复后重新初始化研究库
quant research init-db
```

**备份完整性要求：**
- 每次备份后执行 `pg_dump` 校验（不实际恢复，只验证文件可读）
- 每月第一个周日执行完整恢复演练到临时库
- 恢复演练结果记录到 `/srv/quant-trading/logs/restore-drill.log`

### 5.4 安全清理

```bash
# 日志清理：只操作 /srv/quant-trading/logs/ 下的 .log 文件
find /srv/quant-trading/logs -name "*.log" -mtime +30 -delete

# 快照清理：按清单操作，不递归删除
ls -t /srv/quant-trading/snapshots/ | tail -n +8 | while read dir; do
  [ -f "/srv/quant-trading/snapshots/$dir/manifest.json" ] && \
    rm -rf "/srv/quant-trading/snapshots/$dir"
done
```

### 5.5 监控告警

| 告警 | 检测方式 | 阈值 |
|------|----------|------|
| ETL 连续失败 | `quant data status --limit 3` 全为 FAILED | >= 3 |
| 覆盖率下降 | `DAILY_MARKET_READY` coverage | < 0.99 |
| 磁盘不足 | `df -h` | < 15 GB |
| 数据滞后 | `max(trade_date)` in Gold | 落后 > 2 个交易日 |
| 备份缺失 | `find backups/ -mtime -1` | 无 24h 内备份 |
| 锁死 | lock file 存在时间 | > 10 min |

### 5.6 迁移 DuckDB → PostgreSQL

```bash
# 将本地 DuckDB 中的周频数据迁移到远程 PostgreSQL
quant migrate --from duckdb --to postgresql

# 仅迁移特定表
quant migrate --from duckdb --to postgresql --tables weekly_rotation_score,signal_daily

# 预览但不写入
quant migrate --from duckdb --to postgresql --dry-run
```

---

## 6. 监控

### 6.1 健康检查

```bash
# 各组件状态
docker ps --filter "name=quant-"

# 最近 ETL 运行状态
quant data status --limit 5

# 门禁状态
quant research validate --trade-date latest

# 磁盘空间
df -h /srv/quant-trading/
```

### 6.2 告警规则

| 告警 | 条件 | 动作 |
|------|------|------|
| ETL 连续 3 次失败 | `quant data status` 最近 3 次状态为 FAILED | 检查上游源、网络 |
| 覆盖率下降 | `DAILY_READY` 覆盖率 < 0.99 | 检查上游响应完整性 |
| 磁盘不足 | 剩余空间 < 15 GB | 清理旧快照、旧日志 |
| 数据滞后 | 最新 Gold 数据落后 > 2 个交易日 | 触发手动 `data daily` |

### 6.3 日志查看

```bash
# 调度任务日志
quant scheduler logs --lines 100

# 按日期筛选
quant scheduler logs --date 2026-07-21

# 错误日志
quant scheduler logs --level ERROR

# ETL 运行详情
quant data status --run-id daily-20260721T150000-a1b2c3d4 --json
```

---

## 7. 故障恢复

### 7.1 单个源不可用

某个上游源（如腾讯）不可用时：

1. 数据管道自动将该源标记为 UNAVAILABLE 并在本次任务内熔断
2. 其余源正常的 ETF 不受影响
3. 该源恢复后，下次采集自动重新启用

```bash
# 临时禁用某个源
quant data daily --trade-date 2026-07-21 --disable-source tencent

# 查看当前源状态
quant data status --show-sources
```

### 7.2 ETL 运行失败

```bash
# 查看失败原因
quant data status --limit 1 --json | jq '.[0].message'

# 常见原因和解决：
# "no ETF bars returned for 2026-07-21" → 可能为非交易日，检查日历
# "no bars passed dual-source validation" → 两个源数据都缺失或不一致
# "coverage 0.85 is below required 0.99" → 多个 ETF 缺失，重跑 reconcile

# 手动重跑
quant data reconcile --trade-date 2026-07-21
quant data publish --trade-date 2026-07-21
```

### 7.3 快照恢复异常

```bash
# SHA-256 校验失败
quant data snapshot verify
# 输出不匹配文件列表 → 清除损坏文件后重新拉取
rm -rf data/corrupted_file.parquet
quant data snapshot pull --remote aliyun --profile dev

# DuckDB 视图绑定失败
quant data snapshot verify --fix-views
# 自动重新绑定 Parquet 路径

# 完整重置（清除本地数据，重新拉取）
mv data data.broken.$(date +%Y%m%dT%H%M%S)
quant data snapshot pull --remote aliyun --profile dev
```

### 7.4 磁盘空间耗尽

```bash
# 1. 立即停止采集
quant scheduler stop

# 2. 检查占用
du -sh /srv/quant-trading/data/* | sort -rh | head -10

# 3. 清理
# 删除 7 天前的旧日志
find /srv/quant-trading/logs -name "*.log" -mtime +7 -delete

# 删除 4 个以前的旧快照
ls -t /srv/quant-trading/snapshots/ | tail -n +5 | xargs -I {} rm -rf /srv/quant-trading/snapshots/{}

# 4. 合并小文件（非紧急情况不要跳过）
quant data compact

# 5. 恢复采集
quant scheduler start
```

### 7.5 PostgreSQL 宕机

```bash
# 重启 PostgreSQL
docker restart quant-postgres

# 检查日志
docker logs --tail 50 quant-postgres

# 如果数据损坏，从备份恢复
docker stop quant-postgres
mv /srv/quant-trading/postgresql /srv/quant-trading/postgresql.broken
docker compose --profile server up -d postgresql
docker exec -i quant-postgres pg_restore -U quant -d quant --clean < /srv/quant-trading/backups/quant_20260720.dump
```

---

## 8. 运维命令速查

```bash
# === 服务器 ===
quant scheduler start                           # 启动定时采集
quant scheduler stop                            # 停止定时采集
quant scheduler status                          # 调度状态
quant scheduler logs --lines 50                 # 看日志

quant data status --limit 10                    # 最近 ETL 运行
quant data daily --trade-date 2026-07-21        # 手动采集一天
quant data publish --trade-date 2026-07-21      # 手动发布
quant data snapshot --profile dev               # 创建快照
quant data compact                              # 合并分区

docker compose --profile server restart scheduler  # 重启调度器
docker compose --profile server logs -f scheduler   # 实时日志

# === 研究机 ===
quant data snapshot pull --remote aliyun --profile dev  # 拉取快照
quant data snapshot verify                              # 校验快照
quant research daily-run --trade-date 2026-07-21        # 每日研究
quant research backtest --start 2024-01-01 --end 2026-07-21  # 回测
quant config validate                                    # 校验配置

# === 数据库 ===
docker exec -it quant-postgres psql -U quant             # 进入 psql
quant migrate --from duckdb --to postgresql --dry-run    # 预览迁移
```

# 部署与运维

> 版本: v1.1.0 | 日期: 2026-07-22

正式架构采用“阿里云唯一写入、研究机只读快照”。不要让多台机器同时写同一份
Parquet/DuckDB，也不要在研究命令中临时绕过门禁采集数据。

## 1. 环境与密钥

```bash
uv sync --extra dev
cp .env.example .env
uv run quant config validate
```

常用环境变量：

| 变量 | 用途 |
|------|------|
| `QUANT_DATA_ROOT` | Parquet 与 DuckDB 数据根目录 |
| `QUANT_SNAPSHOT_ROOT` | 本地快照目录 |
| `QUANT_REMOTE_SSH_HOST` | 远程 SSH 别名 |
| `QUANT_REMOTE_SNAPSHOT_ROOT` | 远程快照目录 |
| `QUANT_POSTGRES_DSN` | 审计与研究结果 PostgreSQL DSN |
| `QUANT_TUSHARE_TOKEN` | 股票涨跌停、停牌、ST 与复权数据 |
| `DEEPSEEK_API_KEY` | 可选只读 AI 解释 |

`.env`、Token、DSN、DuckDB、Parquet、快照和生成报告都不得提交到 Git。

## 2. 首次数据初始化

```bash
uv run quant data bootstrap
uv run quant data refresh-master --as-of today
uv run quant data backfill --start 2015-01-01 --end latest
uv run quant data status
```

默认范围来自 `configs/pipeline.yaml`：沪深全市场 ETF、沪深 300、官方交易日历，
同时保存 raw 与 qfq。`--instruments` 只覆盖单次任务，不会修改正式全市场配置。
正式 full-market 运行必须从目标日期的 point-in-time ETF master 解析范围。

增量采集：

```bash
uv run quant data daily --trade-date 2026-07-21
uv run quant data publish --trade-date 2026-07-21 --data-version <VERSION>
```

`daily` 会按 `incremental.overlap_trade_days` 回看交易日。交易日来自锁定的 SSE/SZSE
官方休市公告；节假日不会按简单工作日误判。必需源失败、双源不一致或覆盖率不足
都会阻断，不会静默降级。

## 3. 主题股票数据

板块接口没有可靠历史参数时，只能把实际观察日保存为 point-in-time 快照：

```bash
uv run quant data sector-import --input sectors.csv \
  --snapshot-date 2026-07-21 --source-id akshare_eastmoney

QUANT_TUSHARE_TOKEN=*** uv run quant data stock-backfill \
  --instruments 000001.SZ,600000.SH --start 2024-01-01 --end latest
```

CSV 列固定为 `board_type,board_name,ts_code`，其中 `board_type` 只能是
`concept` 或 `industry`。导入文件 SHA-256 会随快照落库。股票正式门禁要求：

- 每只主题股票具有至少 61 个交易日的 raw/qfq；
- Tushare `stk_limit`、`suspend_d`、`stock_st` 均成功；
- 价格至少有两个独立上游共识；
- 停牌日可由官方停牌记录形成零成交状态行，撮合必定拒绝。

## 4. 快照分发

服务器创建不可变快照：

```bash
uv run quant data snapshot --profile dev --data-version <VERSION> --retain 7
uv run quant data snapshot --profile full --data-version <VERSION> --retain 4
uv run quant data snapshot verify --profile dev
```

研究机拉取并原子恢复：

```bash
uv run quant data snapshot pull --remote aliyun --profile dev
uv run quant data snapshot verify --profile dev
```

manifest 保存 `snapshot_id`、逻辑 `data_version`、profile、文件清单和 SHA-256。
恢复前逐文件校验；失败时不切换当前数据目录。替换来源未知的既有目录必须显式使用
`--backup-existing`。

## 5. 研究、回测与报告

```bash
uv run quant research run --trade-date 2026-07-21 \
  --data-version <VERSION> --output reports/research.json

uv run quant research backtest --strategy etf_rotation \
  --start 2024-01-01 --end 2026-07-21 --data-version <VERSION> \
  --output reports/backtest.json

uv run quant report daily --input reports/research.json
uv run quant report backtest --input reports/backtest.json
```

仅在明确需要集中审计时追加 `--persist-postgres`。未配置 `QUANT_POSTGRES_DSN`
会失败，不会偷偷写入其他数据库。AI 默认禁用，`quant ai status` 不发起请求；
`quant ai evaluate --dimension backtest --input ...` 只发送白名单聚合字段并独立归档。

## 6. Docker 调度

```bash
docker compose config
uv run quant scheduler start
uv run quant scheduler status
uv run quant scheduler logs --lines 200
uv run quant scheduler stop
```

调度容器使用仓库 `docker-compose.yml` 的 `server` profile。容器健康只代表进程
运行；运维验收仍需检查最近 `etl_run`、对应 readiness、Gold 覆盖率和快照 manifest。

## 7. PostgreSQL 迁移

先只读统计：

```bash
uv run quant migrate --dry-run
```

确认 DSN 和表范围后执行：

```bash
QUANT_POSTGRES_DSN='postgresql://...' uv run quant migrate \
  --tables data_readiness,etl_run,quality_issue,snapshot_audit
```

迁移只接受白名单审计表，并使用幂等键写入。研究结果通过 `research run/backtest
--persist-postgres` 写入 `quant_research_run`、`quant_signal_daily` 和
`quant_weekly_rotation_score`。

## 8. 故障处理

| 现象 | 检查 | 处理 |
|------|------|------|
| `DAILY_MARKET_READY` BLOCKED | 必需源、覆盖率、双源共识 | 修复源后 `reconcile`；不直接改 Gold |
| `ROTATION_READY` BLOCKED | 252 日基准、20 日双源、日历 | 回填缺失数据并重新发布版本 |
| `STOCK_BACKTEST_READY` BLOCKED | Tushare 权限、61 日历史、主题快照 | 修复权限/范围；不得用当前成员补历史 |
| `SOURCE_MISMATCH` | Silver 冲突与 Raw SHA | 等官方收盘数据或增加独立源裁决 |
| writer lock | 是否有存活的写任务 | 等待任务完成；确认无进程后再人工处理锁 |
| 快照校验失败 | manifest 与文件 SHA | 重新传输；不要强制恢复损坏快照 |
| 磁盘不足 | data/snapshots 容量 | 先归档旧 full 快照，再运行 compact |

任何更正都应从 Raw/Bronze 重算并产生新的逻辑 `data_version`。Gold 不原地更新。

## 9. 发布验收清单

```bash
uv run ruff check .
uv run mypy src/quant_trading
uv run quant config validate
uv run quant research strategies validate
docker compose config
```

生产验收还必须记录：全市场标的数、日期范围、Raw/Gold 行数、各门禁状态、
`snapshot_id`、`data_version`、manifest SHA-256、Git commit，以及 PostgreSQL 实际
写入/读取证据。没有这些证据时只能称为“实现完成”，不能称为“生产就绪”。

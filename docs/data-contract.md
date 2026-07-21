# 数据契约

> 版本: v1.0.0 | 日期: 2026-07-21

本文档定义 `data/` 管道层与 `research/` 策略层之间的数据契约。
管道层负责生产已验证数据，策略层只消费通过门禁的数据。
两层之间通过本契约解耦——只要契约不变，管道和策略可以独立演进。

---

## 1. 核心原则

### 1.1 门禁阻断

策略层在消费数据前必须显式检查门禁状态。门禁未通过时，策略**不得**静默降级，
必须抛出异常或返回明确的"不可用"状态。

```python
# 策略层消费数据的标准前置检查
from quant_trading.data.readiness import require_readiness

require_readiness("2026-07-21", "ROTATION_READY")
# 不通过时抛出 ReadinessError，策略停止
```

### 1.2 点-in-time 安全

任何交易日只能使用该日及之前落库的数据。禁止用"当前已知"的全量信息回填历史。

- 正式模式：`point_in_time` — 每个交易日读取该日及之前最近一次快照
- 探索模式：`fixed_current_universe` — 使用当前成分回测历史，报告必须带幸存者偏差警告

### 1.3 来源可审计

每个融合字段必须保留来源。多源非空值冲突时保留高优先级值并记录 `conflict:<field>`。
正式信号门禁保持关闭，直到冲突解决或显式排除。

### 1.4 不可覆盖

已落库的 Gold 数据不可覆盖。同一主键的新数据必须通过新版本写入，
旧版本保留用于审计回放。

---

## 2. 数据分级

### 2.1 四级流水线

```
Raw ────────→ Bronze ────────→ Silver ────────→ Gold
原始 HTTP    字段规范化       单源记录          多源共识
```

| 阶段 | 存储 | 写入规则 | 内容 |
|------|------|----------|------|
| Raw | `data/raw/<source>/<dataset>/<fetch_date>/<run_id>/` | 追加 | 原始 HTTP 响应 gzip + 请求元数据 + SHA-256 |
| Bronze | `data/bronze/<dataset>/year=YYYY/month=MM/` | 主键覆盖 | 字段规范化、代码统一、去重 |
| Silver | `data/silver/<dataset>/year=YYYY/month=MM/` | 追加 | 单源记录、冲突标记 |
| Gold | `data/gold/<dataset>/year=YYYY/month=MM/` | 不可覆盖 | 多源共识、PASS/PASS_OFFICIAL |

### 2.2 质量状态

| 状态 | 含义 | 进入条件 |
|------|------|----------|
| `PASS` | 多源一致 | >= 2 个独立来源，所有校验字段通过 |
| `PASS_OFFICIAL` | 官方裁决 | 交易所/基金公司官方数据确认 |
| `PROVISIONAL` | 单源暂存 | 仅 1 个来源，可用于探索但不进入正式信号 |
| `STALE` | 过期 | 超过 `max_stale_days` 未更新的已发布数据 |
| `QUARANTINED` | 隔离 | 多源冲突且无法仲裁 |
| `UNSUPPORTED` | 不支持 | 该数据类别尚无可用来源 |

### 2.3 阶段字段的通用规则

Bronze/Silver/Gold 是管道阶段，不是数据集 schema。每个阶段的字段由两部分组成：
**通用列**（所有数据集都有）+ **业务列**（数据集特定）。

**通用列（每个阶段都有）：**

```
Raw 阶段：source_id, dataset, instrument_id, fetched_at, status_code, raw_hash, request_params
Bronze 阶段：instrument_id, <业务字段>, source_id, available_at, raw_hash, schema_version
Silver 阶段：= Bronze 字段 + quality_status, quality_flags, conflict_detail
Gold 阶段：  = 去掉 source_id/raw_hash，+ source_count, source_map, quality_flags
```

**业务列（按数据集不同）：**

| 数据集 | Bronze/Silver 特有字段 | 说明 |
|--------|----------------------|------|
| `etf_daily_bar` | OHLCV + amount + turnover + adjustment | ETF 日线 |
| `stock_daily_bar` | OHLCV + amount + turnover + is_st + is_suspended | 股票日线 |
| `index_daily_bar` | OHLCV + amount | 指数日线，无 volume/turnover |
| `stock_intraday_5m` | OHLCV + amount + bar_time | 股票 5 分钟线，时间精度到分钟 |
| `etf_master_scd` | name + exchange + listing_date + ... + effective_date | 缓慢变化维，无 OHLCV |
| `etf_nav` | nav + acc_nav + nav_date | ETF 净值 |
| `trade_calendar` | is_trade_day + next_trade_day + prev_trade_day | 交易日历 |
| `sector_flow` | board_type + board_name + net_flow + main_net_flow | 板块资金流向 |
| `sector_constituent` | board_type + board_name + ts_code | 板块-股票映射 |
| `dragon_tiger` | ts_code + reason + net_buy + buy_amount + sell_amount | 龙虎榜 |

**Gold 阶段的通用约束：**
- 去掉 `source_id`、`raw_hash`、`quality_status`（来源细节留在 Silver）
- 新增 `source_count`（通过校验的来源数）、`source_map`（哪些源通过）、`quality_flags`
- 写入规则：**不可覆盖**，同一主键只允许 insert，不允许 update
- 每个数据集有独立的校验规则（见第 3 节各数据集定义）

### 2.4 Gold 数据版本与更正机制

#### 2.4.1 版本字段

所有 Gold 数据集除业务主键外，统一包含以下版本字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data_version` | str | 数据版本标识（通常为生成此版本的 `run_id`） |
| `published_at` | datetime | 首次发布时间（Asia/Shanghai） |
| `schema_version` | str | 写入时的 schema 版本号 |

**Gold 完整主键：`(<业务主键>, data_version)`**

以 `etf_daily_bar` 为例：`(instrument_id, trade_date, adjustment, data_version)`

#### 2.4.2 版本产生场景

| 场景 | 触发 | 旧版本处理 | 新 data_version |
|------|------|-----------|----------------|
| 首次发布 | 日常 `publish` | — | 当日 run_id |
| 数据更正 | 源数据修正后 `reconcile` + `publish` | 保留 | 新 run_id |
| 官方裁决 | 交易所/基金公司正式数据到达 | 保留 | `official-<date>` |
| 重新发布 | 校验规则升级后重跑历史 | 保留 | `revalidate-<run_id>` |

#### 2.4.3 默认查询规则

```sql
-- 默认视图只返回每个业务主键的最新版本
CREATE VIEW v_etf_daily_verified AS
SELECT DISTINCT ON (instrument_id, trade_date, adjustment)
    *
FROM gold_etf_daily_bar
ORDER BY instrument_id, trade_date, adjustment, published_at DESC;
```

明确需要历史版本时，查询方指定 `data_version`：

```sql
SELECT * FROM gold_etf_daily_bar
WHERE data_version = 'daily-20260721T080000-a1b2c3d4';
```

#### 2.4.4 回放流程

```python
# 按 data_version 完整回放某个时点的数据
bars = get_gold_bars(
    instruments=ROTATION_CANDIDATES,
    start="2026-01-01", end="2026-07-21",
    data_version="daily-20260721T080000-a1b2c3d4",  # 锁定版本
)
```

快照 manifest 中的 `data_version` 与 Gold 记录的 `data_version` 一致，
保证从快照恢复后的默认查询返回与快照创建时完全相同的数据。

### 2.5 数据采集范围契约

数据采集范围与策略研究范围必须分离，不能因为当前轮动策略只有少量候选，
就把基础行情裁剪为候选池。

| 范围 | 配置或入口 | 作用 | 是否改变正式基础范围 |
|------|------------|------|----------------------|
| 全市场基础范围 | `configs/pipeline.yaml` 的 `collection.scope` | 决定生产管道采集哪些资产 | 是 |
| 策略研究范围 | `configs/universes.yaml` | 决定策略在已采集数据中研究哪些标的 | 否 |
| 单次任务覆盖 | CLI `--instruments` | 补数、重试或排障 | 否，仅本次任务 |
| 门禁范围 | 策略的 `required_readiness` | 检查运行策略所需数据是否完备 | 否 |

正式默认值为 `full_market`：生产管道根据 `etf_master_scd` 在目标日期的
point-in-time 状态采集上交所和深交所全部 ETF。新增、退市和历史回填均使用
目标日期当时的成员范围，禁止拿今天的 ETF 清单回填历史。

```yaml
# configs/pipeline.yaml
collection:
  asset_types: [etf]
  scope: full_market
  exchanges: [SSE, SZSE]
  include_delisted: true
  adjustments: [raw, qfq]
  datasets:
    - etf_master
    - etf_daily_bar
    - index_daily
    - trade_calendar
  benchmarks: ["000300.SH"]

history:
  start: "2015-01-01"
  end: latest_trade_date
  respect_listing_date: true

incremental:
  overlap_trade_days: 5
```

范围解析优先级为：CLI `--instruments` > `configured_instruments` >
`etf_master_scd` point-in-time 全市场范围。高优先级只覆盖当前任务，不得回写配置。

```bash
# 正式全市场历史回填
quant data backfill --start 2015-01-01 --end latest

# 仅为指定 ETF 补数，不修改正式范围
quant data backfill --instruments 159875.SZ,516160.SH \
  --start 2024-01-01 --end 2026-07-21
```

`DAILY_MARKET_READY` 检查全市场当日覆盖，`ROTATION_READY` 检查配置候选池，
`CROSS_SECTION_READY` 检查满足上市天数条件的 point-in-time 全市场历史。

---

## 3. 数据集定义

### 3.1 ETF 日线行情 (`etf_daily_bar`)

#### 3.1.1 价格口径：raw 与 qfq

- `raw` 是未复权价格，即目标交易日市场真实展示和可成交的价格。
- `qfq` 是前复权价格，历史价格按分红、拆分等除权事件调整，用于获得连续的
  收益和趋势序列；它不一定是当时真实可成交价格。

两种口径必须同时保留，并在每次读取时显式指定 `adjustment`。接口不得提供会
悄悄选择某种口径的默认值。

| 场景 | 必须使用 | 原因 |
|------|----------|------|
| 动量、均线、波动率、最大回撤 | `qfq` | 避免除权事件产生虚假跳变 |
| 策略评分、排名和信号生成 | `qfq` | 保持跨期收益可比 |
| 订单价格、成交价格、现金占用 | `raw` | 还原真实可交易价格 |
| 涨跌停、手数和成交审计 | `raw` | 必须符合当日市场约束 |

硬规则：禁止用 `qfq` 模拟成交；禁止用 `raw` 直接计算跨除权日收益。
典型链路为“T 日 `qfq` 收盘生成目标仓位，T+1 使用 `raw` 开盘撮合”。

前复权因子会随未来分红和数据源修订发生变化，因此正式回测读取 `qfq` 时必须
同时锁定 `data_version` 或 `snapshot_id`。仅指定日期范围不足以保证回放一致。

**Bronze/Silver 字段（业务列）：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `instrument_id` | str | 统一代码，如 `510300.SH` |
| `trade_date` | date | 交易日 |
| `pre_close` | float | 前收盘价 |
| `open` | float | 开盘价 |
| `high` | float | 最高价 |
| `low` | float | 最低价 |
| `close` | float | 收盘价 |
| `volume` | int | 成交量（份），统一为份而非手 |
| `amount` | float | 成交额（元） |
| `turnover_rate` | float | 换手率（%），部分源可能缺失 |
| `trading_status` | str | `NORMAL`/`SUSPENDED`/`LIMIT_UP`/`LIMIT_DOWN` |
| `adjustment` | str | 复权类型：`raw`/`qfq` |
| `adjustment_source` | str | 复权因子来源，如 `tdx_factor`/`tencent_qfq` |
| `adjustment_date` | date | 复权截止日 |
| `adjustment_version` | str | 复权因子版本哈希 |

**Gold 字段（通用列 + 业务列）：**

去掉 Bronze/Silver 中的 `source_id`、`raw_hash`、`quality_status`，保留所有业务列，新增：

| 字段 | 类型 | 说明 |
|------|------|------|
| `source_count` | int | 通过校验的独立来源数 |
| `source_map` | str(JSON) | `{"eastmoney_kline_raw": true, "tencent_kline_raw": true}` |
| `amount_source_count` | int | 成交额的独立来源数 |
| `quality_flags` | str(JSON) | 质量标记，如 `["amount_single_source"]` |

**Gold 完整主键：** `(instrument_id, trade_date, adjustment, data_version)`

**校验规则：**

| 检查项 | 规则 | 失败动作 |
|--------|------|----------|
| OHLC 双源对账 | `abs(close_a - close_b) <= 0.001` 元 | 隔离 |
| 成交量双源对账 | `abs(vol_a - vol_b) / max(vol_a, vol_b) <= 0.001` | 隔离 |
| 成交额双源对账 | `abs(amt_a - amt_b) / max(amt_a, amt_b) <= 0.03` | 标记 `amount_single_source` |
| 价格范围 | `low <= open/close/high` 且全部 > 0 | 隔离 |
| 非负检查 | 量、额 >= 0 | 隔离 |
| 前复权交叉验证 | 腾讯/同花顺前复权 vs 通达信未复权×除权因子 | 不一致则隔离 |

**来源独立性判定：**
同一底层域名/接口包装的不同适配器算一个来源。例如 AKShare(东方财富) 和 EastmoneySource
都依赖 `push2his.eastmoney.com`，在双源对账中合计为 **1** 个来源。

### 3.2 ETF 全市场清单 (`etf_master_scd`)

缓慢变化维（SCD Type 2），每个变更产生新行。

**Silver 字段（逐来源记录）：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `instrument_id` | str | 统一代码 |
| `code` | str | 原始代码 |
| `exchange` | str | `SH` / `SZ` |
| `name` | str | 基金名称 |
| `etf_category` | str | 类别：`stock`/`bond`/`commodity`/`money`/`qdii` |
| `currency` | str | 交易币种，默认 `CNY` |
| `settlement_cycle` | str | 交收周期 |
| `tracking_index_code` | str | 跟踪指数代码 |
| `tracking_index_name` | str | 跟踪指数名称 |
| `fund_company` | str | 基金公司 |
| `listing_date` | date | 上市日期 |
| `delisting_date` | date | 退市日期（活跃为 null） |
| `management_fee` | float | 管理费率 |
| `custodian_fee` | float | 托管费率 |
| `effective_date` | date | 此快照生效日期 |
| `source_id` | str | 来源标识 |
| `fetched_at` | datetime | 抓取时间 |
| `quality_status` | str | 单源质量状态 |

**Silver 主键：** `(instrument_id, effective_date, source_id)`

**Gold 字段（多源融合记录）：**

去掉 `source_id`、`fetched_at`、`quality_status`，新增：

| 字段 | 类型 | 说明 |
|------|------|------|
| `source_count` | int | 通过校验的来源数 |
| `source_map` | str(JSON) | `{"eastmoney_snapshot": true, "tencent_master_*": false}` |
| `valid_from` | date | 记录生效日期（等于 earliest effective_date） |
| `valid_to` | date | 记录失效日期（下一条记录的 valid_from - 1 天；当前记录为 `9999-12-31`） |
| `is_current` | bool | 是否当前有效记录 |
| `data_version` | str | 版本标识 |
| `published_at` | datetime | 发布时间 |
| `schema_version` | str | Schema 版本 |

**Gold 主键：** `(instrument_id, valid_from, data_version)`

**SCD 语义：**

```sql
-- 查询某个 point-in-time 的 ETF 清单
SELECT * FROM gold_etf_master_scd
WHERE valid_from <= '2026-07-21'
  AND valid_to >= '2026-07-21'
  AND is_current = true;

-- 当前活跃 ETF 清单
SELECT * FROM gold_etf_master_scd
WHERE is_current = true AND delisting_date IS NULL;
```

退市 ETF 的 `delisting_date` 被设置后，下一次发布时 `valid_to` 更新为退市日期，
`is_current` 保持 `true` 直到 `valid_to < CURRENT_DATE`，之后新版本中 `is_current = false`。

### 3.3 ETF 日频特征 (`etf_features_daily`)

| 字段 | 类型 | 说明 |
|------|------|------|
| `instrument_id` | str | 统一代码 |
| `trade_date` | date | 交易日 |
| `ret_1d` | float | 1 日收益 |
| `ret_5d` | float | 5 日收益 |
| `ret_20d` | float | 20 日收益 |
| `ret_60d` | float | 60 日收益 |
| `volatility_20d` | float | 20 日年化波动率 |
| `drawdown_60d` | float | 60 日最大回撤 |
| `avg_amount_20d` | float | 20 日均成交额 |
| `avg_volume_20d` | float | 20 日均成交量 |
| `ma20` | float | 20 日均价 |
| `ma60` | float | 60 日均价 |
| `ma20_distance` | float | 收盘价相对 MA20 偏离度 |
| `relative_strength_20d` | float | 相对沪深 300 的 20 日超额收益 |
| `relative_strength_60d` | float | 相对沪深 300 的 60 日超额收益 |
| `amount_ratio_5_20` | float | 5 日成交额 / 20 日成交额 |
| `nav_premium` | float | 溢价率（净值状态为 PROVISIONAL 时为 null） |
| `nav_premium_quality` | str | 溢价率质量：`PROVISIONAL`/`PASS_OFFICIAL` |
| `published_at` | datetime | 发布时间 |
| `data_version` | str | 数据版本（run_id） |

**Gold 主键：** `(instrument_id, trade_date)`

**计算依赖：** 只使用 Gold qfq 价格。上市不足 61 个交易日的 ETF 特征标为 `null`。
最后交易日为零成交且之后无双源行情的 ETF，特征列标记"不可评估"。

### 3.4 沪深 300 日线 (`index_daily_verified`)

| 字段 | 类型 | 说明 |
|------|------|------|
| `instrument_id` | str | `000300.SH` |
| `trade_date` | date | 交易日 |
| `open` | float | 开盘 |
| `high` | float | 最高 |
| `low` | float | 最低 |
| `close` | float | 收盘 |
| `volume` | int | 成交量 |
| `amount` | float | 成交额 |
| `source_count` | int | 校验通过的来源数 |

**要求：** 至少 2 个独立来源，价格容忍度 0.01 元（指数点位）。

### 3.5 交易日历 (`trade_calendar_verified`)

| 字段 | 类型 | 说明 |
|------|------|------|
| `calendar_date` | date | 日期 |
| `is_trade_day` | bool | 是否为交易日 |
| `next_trade_day` | date | 下一交易日（非交易日为 null） |
| `prev_trade_day` | date | 上一交易日（非交易日为 null） |
| `source_count` | int | 校验来源数 |
| `sources` | str(JSON) | `{"sse": true, "szse": true}` |

**要求：** 至少 2 个独立来源（上交所公告 + 深交所公告）。两个来源的交易日判定必须完全一致。

### 3.6 主题成分快照 (`sector_constituent_snapshot`)

| 字段 | 类型 | 说明 |
|------|------|------|
| `snapshot_date` | date | 快照日期 |
| `board_type` | str | 板块类型：`concept`/`industry` |
| `board_name` | str | 板块名称 |
| `ts_code` | str | 股票代码 |
| `source_id` | str | 来源 |

**主键：** `(snapshot_date, board_type, board_name, ts_code)`

**注意：** AKShare 板块排行没有历史参数。旧周缺少已落库快照时，不使用当前成分伪造历史 Top 5。

---

## 4. 门禁体系

### 4.1 设计原则

- **门禁按数据集独立**。沪深 300 缺失不影响 ETF 行情发布
- **策略声明所需门禁**。每个策略在配置中声明自己依赖哪些门禁，不依赖全局统一门禁
- **候选数量动态计算**。门禁阈值为 `required_count`（来自 point-in-time 配置），不硬编码 `15/15`
- **缺失来源 != 阻断**。非必需来源不可用时标记为 DEGRADED，不阻断 READY

### 4.2 门禁清单

| 门禁 | 检查范围 | 阻断影响 |
|------|----------|----------|
| `DAILY_MARKET_READY` | 当日 ETF 行情采集、双源校验、覆盖率 | `publish` 失败，特征因子不更新 |
| `FEATURE_READY` | 指定窗口的 qfq Gold 完整性和时序连续性 | 特定窗口特征因子不可用 |
| `ROTATION_READY` | 候选 ETF（数量来自 config）的 raw/qfq Gold + 沪深 300 + 交易日历 | 周频 ETF 轮动信号不生成 |
| `CROSS_SECTION_READY` | 全市场 ETF 的 61 日双源历史 | 全市场横截面策略不可用 |
| `STOCK_BACKTEST_READY` | 涨跌停、停复牌、ST 数据权限 | 股票级别回测不可用 |

### 4.3 门禁状态机

```
                    ┌─────────────┐
                    │   PENDING   │  ← 初始，尚无数据
                    └──────┬──────┘
                           │ 首次数据到达
                           ▼
                    ┌─────────────┐
                    │ PROVISIONAL │  ← 未满足全部条件
                    └──────┬──────┘
                           │ 全部条件通过
                           ▼
                    ┌─────────────┐
                    │    READY    │
                    └──────┬──────┘
                           │ 新数据未通过，或必需来源缺失
                           ▼
              ┌──────────┴──────────┐
              ▼                     ▼
       ┌─────────────┐       ┌─────────────┐
       │   BLOCKED   │       │  DEGRADED   │
       │ 必需源缺失   │       │ 非必需源缺失 │
       └─────────────┘       └─────────────┘
```

### 4.4 门禁详细定义

#### DAILY_MARKET_READY

| 条件 | 阈值 | 类型 |
|------|------|------|
| Gold 行情覆盖率（当日已发布 / 前一日全市场 ETF 数） | `>= 0.99` | 阻塞 |
| 东方财富当日行情非空 | 不为空 | 阻塞 |
| 至少 2 个独立来源通过 ETF 日线校验 | `source_count >= 2` | 阻塞 |
| 腾讯源可用或被显式禁用 | — | 非阻塞（标记 DEGRADED） |

**不通过时：** `publish` 失败，特征因子不更新。不依赖 `CROSS_SECTION_READY`。

#### FEATURE_READY

| 条件 | 阈值 | 类型 |
|------|------|------|
| 目标窗口的 qfq Gold 无缺失日 | 连续交易日覆盖率 100% | 阻塞 |
| 目标窗口的未复权 Gold 无缺失日 | 同上 | 阻塞 |
| 窗口长度满足策略最低要求 | `>= min_history_bars` | 阻塞 |

#### ROTATION_READY

| 条件 | 阈值 | 类型 |
|------|------|------|
| 候选 ETF raw/qfq Gold 覆盖 | `published_count >= required_count`（required_count = 配置中的候选数） | 阻塞 |
| 沪深 300 Gold 历史 | `>= 252` 个交易日 | 阻塞 |
| 交易日历双源一致 | `100%` 无冲突日 | 阻塞 |
| 各候选 20 日双源覆盖率 | `>= 0.98` | 阻塞 |
| 各候选收益绝对值的双源差异 | `<= 0.003` | 阻塞 |

#### CROSS_SECTION_READY

| 条件 | 阈值 | 类型 |
|------|------|------|
| 全市场 ETF raw/qfq Gold | 上市 >= 61 日的 ETF 均具备 >= 61 日双源 Gold | 阻塞 |
| 上市不足 61 日 | 标记为"不可评估" | 非阻塞 |
| 已退市 ETF | 退市前的历史完整 | 非阻塞 |

#### STOCK_BACKTEST_READY

| 条件 | 阈值 | 类型 |
|------|------|------|
| `stk_limit` 接口可用 | 可获取涨跌停价 | 阻塞 |
| `suspend_d` 接口可用 | 可获取停复牌状态 | 阻塞 |
| `stock_st` 接口可用 | 可获取 ST 标记 | 阻塞 |

### 4.5 策略声明所需门禁

```yaml
# configs/strategies/etf_rotation.yaml
name: etf_rotation
enabled: true
required_readiness:
  - ROTATION_READY
```

研究运行器在创建策略后、执行前检查其 `required_readiness`。注册表校验配置中
声明的门禁是否真实存在。

### 4.6 门禁查询接口

```python
from quant_trading.data.readiness import (
    require_readiness,
    get_readiness,
    list_blocking_issues,
)

# 阻塞式检查
require_readiness("2026-07-21", "ROTATION_READY")

# 非阻塞查询
status = get_readiness("2026-07-21", "ROTATION_READY")

# 列出阻断项
issues = list_blocking_issues("2026-07-21", "ROTATION_READY")
```

---

## 4.7 数据源失败语义

### 4.7.1 失败分类

每个数据源的每次采集产生明确的终态。调用方能够根据终态判断
应重试、阻断、还是标记为非必需缺失。

| 失败类型 | 终态 | 含义 | 动作 |
|----------|------|------|------|
| `NETWORK_ERROR` | `FAILED` | DNS/TCP/SSL 不可达 | 重试（指数退避，最多 3 次） |
| `HTTP_ERROR_5XX` | `FAILED` | 上游服务器错误 | 重试（退避，最多 2 次） |
| `HTTP_ERROR_4XX` | `UNAVAILABLE` | 认证失败或接口不存在 | 不重试，标记源不可用，告警 |
| `RATE_LIMITED` | `THROTTLED` | 触发限流 | 等待窗口后重试 |
| `EMPTY_RESPONSE` | `NO_DATA` | 源返回空（可能是非交易日） | 不重试，区分"真无数据" vs "源缺失" |
| `PARSE_ERROR` | `QUARANTINED` | 响应格式变化 | 隔离，人工检查 |
| `TIMEOUT` | `FAILED` | 响应超时 | 重试 1 次（加长超时） |
| `DISK_SPACE_LOW` | `BLOCKED` | 磁盘低于 `min_free_gb` | 所有采集停止，告警 |

### 4.7.2 来源必需性

```yaml
# configs/pipeline.yaml
sources:
  eastmoney:
    required: true          # 必须可用，否则 DAILY_MARKET_READY 阻断
  tencent:
    required: false         # 不可用时 DEGRADED，不阻断
    fallback_ttl_hours: 24  # 上次可用数据的有效期
  ths:
    required: true          # 成交额依赖同花顺，ROTATION_READY 需要
  tdx:
    required: true          # 前复权因子依赖通达信
  baostock:
    required: false
    max_stale_days: 7
  tushare:
    required: false
    note: "增强源，仅用于股票信息补充"
  akshare:
    required: false
    note: "板块发现源，不影响 ETF 门禁"
```

### 4.7.3 熔断与恢复

```python
# 单次任务内，同一源连续失败 N 次后熔断
CIRCUIT_BREAKER = {
    "max_consecutive_failures": 5,
    "cooldown_minutes": 30,
    "half_open_probe": 1,       # 冷却后尝试 1 次探测
}
```

不可用源在单次 `data daily` 任务内自动熔断，下次任务重新探测。
连续 3 次任务均熔断的源，需人工确认后重新启用。

### 4.7.4 静默降级禁止

正式模式（`QUANT_MARKET_MODE=live` 或 consumed via snapshot）下：
- 不得以公开源或单源结果替代指定的必需源
- 不得在源不可用时使用上一期缓存数据伪装为当日数据
- 不得将 PROVISIONAL 数据标记为 PASS 以满足覆盖率门禁

---

## 5. 快照契约

### 5.1 快照目录结构

```
snapshots/
└── <snapshot_id>/
    ├── manifest.json         # 文件清单 + SHA-256
    ├── data/
    │   ├── catalog/
    │   │   └── quant.duckdb  # DuckDB 目录（视图 + 就绪状态）
    │   ├── bronze/
    │   ├── silver/
    │   └── gold/
    └── profiles/
        ├── dev/              # dev 快照不含 raw/
        └── full/             # full 快照含完整 raw/
```

### 5.2 manifest.json

```json
{
  "snapshot_id": "20260721T032309084199Z-a1b2c3d4",
  "profile": "dev",
  "created_at": "2026-07-21T03:23:09+08:00",
  "data_version": "daily-20260721T032000-x1y2z3w4",
  "host": "aliyun",
  "git_commit": "abc123def456",
  "duckdb_checkpoint_lsn": "1-2-3",
  "files": [
    {
      "path": "data/catalog/quant.duckdb",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "size": 1048576
    }
  ]
}
```

### 5.3 拉取校验流程

```
本地请求快照
  │
  ├─ SSH 连接 aliyun
  ├─ rsync manifest.json
  ├─ 对比本地已缓存文件列表
  ├─ rsync 仅传输变更文件
  ├─ 逐文件校验 SHA-256
  ├─ 校验 manifest 完整性（文件数一致）
  ├─ DuckDB checkpoint 校验
  └─ 原子切换 latest 指针
```

### 5.4 安全约束

- 阿里云是**唯一写入端**。任何研究机不得反向 rsync
- 不得通过 SSHFS/NFS 直接打开服务器上的 DuckDB
- 不得在服务器上运行研究命令（`research` 子命令在服务器上被禁用）
- 快照恢复后，DuckDB 视图中的 Parquet 路径自动绑定到本地绝对路径
- 包含本地写入或无法验证来源的 `data/` 目录，默认拒绝覆盖。使用 `--backup-existing` 强制替换（旧目录先重命名备份）

---

## 6. 跨层接口

### 6.1 策略层消费黄金数据

```python
from quant_trading.data.readiness import require_readiness
from quant_trading.data.storage import get_gold_bars, get_next_trade_day

def run_weekly_rotation(
    trade_date: date,
    *,
    data_version: str,
) -> WeeklyResult:
    # 1. 门禁检查
    require_readiness(trade_date, "ROTATION_READY")

    # 2. 读取已验证数据
    bars = get_gold_bars(
        instruments=ROTATION_CANDIDATES,
        start=trade_date - timedelta(days=252),
        end=trade_date,
        adjustment="qfq",
        data_version=data_version,
    )
    benchmark = get_gold_index_bars("000300.SH", start=..., end=...)
    calendar = get_trade_calendar(start=..., end=...)

    # 3. 策略计算（纯函数，不依赖数据源）
    features = compute_features(bars)
    signals = generate_signals(features, benchmark)

    # 4. 执行价格使用 raw（未复权）
    execution_date = get_next_trade_day(trade_date, data_version=data_version)
    execution_prices = get_gold_bars(
        instruments=[s.instrument_id for s in signals],
        start=execution_date,
        end=execution_date,
        adjustment="raw",
        data_version=data_version,
    )

    return WeeklyResult(...)
```

### 6.2 错误处理契约

| 异常 | 触发条件 | 调用方处理 |
|------|----------|-----------|
| `ReadinessError` | 门禁未通过 | 停止策略，记录日志，不生成信号 |
| `NoDataError` | 指定区间无 Gold 数据 | 缩小日期范围重试或报错 |
| `CoverageWarning` | 覆盖率在阈值边缘 | 可继续但报告标出低覆盖数据点 |
| `StaleDataWarning` | 数据超过 max_stale_days | 可继续但报告标出数据滞后 |

### 6.3 配置版本管理

```yaml
# configs/universes.yaml 变更时，version 必须递增
version: "weekly-rotation-v2.1.0"  # v2.0.0 → v2.1.0: 新增 CPO 候选 ETF
```

周频信号写入 PostgreSQL 时携带 `strategy_version`。
版本变更自动触发全量信号重算，旧版本信号保留用于对比。

---

## 7. 数据源注册

### 7.1 添加新数据源

```python
# src/quant_trading/data/providers.py
from quant_trading.data.providers import BarSource

class NewProviderSource(BarSource):
    source_id: str = "new_provider_kline_raw"       # 全局唯一
    upstream_domain: str = "api.newprovider.com"     # 用于来源独立性判定
    supports_adjustments: tuple[str, ...] = ("raw",) # 支持的复权类型
    rate_limit_rps: float = 1.0                      # 每秒请求数

    def fetch_history(self, instrument_id, start, end, **kwargs):
        ...
```

### 7.2 来源独立性判定

在 `validation.py` 中注册来源分组：

```python
SOURCE_UPSTREAM_GROUPS = {
    "eastmoney": ["eastmoney_kline_raw", "eastmoney_kline_qfq", "akshare_eastmoney"],
    "tencent": ["tencent_kline_raw", "tencent_kline_qfq"],
    "ths": ["ths_kline_qfq"],
    "tdx": ["tdx_kline_raw", "tdx_kline_qfq"],
    "baostock": ["baostock_kline_raw"],
}
```

双源对账时，同一 `upstream_group` 的多个 source_id 合并为 1 个独立来源。

# 开发 TODO 契约

> 本清单是代码内 `TODO(...)` 的唯一详细说明。后续 agent 应按依赖顺序实现，
> 不得绕过数据门禁、point-in-time 边界或 raw/qfq 价格契约。

## 使用规则

1. 开始前读取对应 TODO、数据契约和架构章节。
2. 一次变更只处理一个主 TODO；依赖未完成时不得用假数据冒充完成。
3. 验收证据必须可复现且离线，至少包括 Ruff、mypy、相关 CLI smoke 输出。
4. 涉及策略的变更还要保存锁定 `snapshot_id`/`data_version` 的前后对比。
5. 完成后删除代码内 TODO，并在本清单把状态改为 `DONE`，记录实现文件。

## Phase 1：配置与领域模型

### P1-CONFIG-01 — 完整配置模型

- 状态：`OPEN`
- 文件：`src/quant_trading/config.py`
- 依赖：无
- 实现：为 pipeline、universes、backtest、reporting 建立严格 Pydantic 模型；
  校验数据范围、权重合计、费用和 raw/qfq 组合。
- 硬约束：未知字段报错；密钥只从环境变量读取；禁止静默默认交易费用。
- 验收：所有仓库 YAML 可加载；错误字段、负费用、错误价格口径明确失败。

### P1-MODEL-01 — 行情审计字段

- 状态：`OPEN`
- 文件：`models/bar.py`
- 实现：补齐前收盘、交易状态、来源计数、来源映射、复权因子版本和质量标记。
- 硬约束：Gold 完整键包含 `instrument_id/trade_date/adjustment/data_version`。
- 验收：模型覆盖 `docs/data-contract.md` 的 ETF 日线 Gold 字段。

### P1-MODEL-02 — 信号追溯字段

- 状态：`OPEN`
- 文件：`models/signal.py`
- 实现：补齐稳定 signal_id、执行日期、策略配置哈希、因子归因和生成时间。
- 验收：任一目标仓位可追溯到策略版本、数据版本和输入配置。

### P1-MODEL-03 — A 股订单状态机

- 状态：`OPEN`
- 文件：`models/order.py`
- 实现：补齐 TTL、限价、部分成交剩余量、拒绝原因枚举和状态转换校验。
- 硬约束：T 日信号只能在 T+1 或之后执行，成交价格必须为 raw。

### P1-MODEL-04 — 成交费用审计

- 状态：`OPEN`
- 文件：`models/trade.py`
- 实现：拆分佣金、最低佣金、印花税、滑点和成交金额。
- 验收：费用分项之和等于总费用，ETF 不收股票印花税。

### P1-MODEL-05 — 组合账本

- 状态：`OPEN`
- 文件：`models/position.py`
- 实现：补齐可用/冻结数量、raw 成本、市值、现金、净值和数据日期。
- 验收：每个交易日满足现金与持仓账本守恒。

## Phase 2：数据管道

### P2-DATA-01 — 正式数据源适配器

- 状态：`OPEN`
- 文件：`data/providers.py`
- 依赖：P1-CONFIG-01、P1-MODEL-01
- 实现：接入文档指定的数据源，加入分类重试、限流、熔断和原始响应 SHA-256。
- 硬约束：同一上游包装不能算两个独立来源；网络失败不能解释为空数据。

### P2-DATA-02 — Raw 到 Gold 编排

- 状态：`OPEN`
- 文件：`data/pipeline.py`
- 依赖：P2-DATA-01、P2-DATA-03、P2-DATA-05
- 实现：完成 full-market bootstrap/daily/backfill/reconcile/publish 和幂等 run_id。
- 验收：固定输入重复运行产生相同 Gold 与 manifest 哈希。

### P2-DATA-03 — 多源校验与融合

- 状态：`OPEN`
- 文件：`data/validation.py`
- 实现：实现 OHLCV、成交额、复权因子、日历和来源独立性校验。
- 硬约束：冲突数据隔离；字段级来源可追溯；禁止把 PROVISIONAL 提升为 PASS。

### P2-DATA-04 — 门禁计算与持久化

- 状态：`OPEN`
- 文件：`data/readiness.py`
- 依赖：P2-DATA-03、P2-DATA-05
- 实现：计算并保存全部门禁、覆盖率和阻断项，提供阻塞/非阻塞查询。
- 验收：候选数量来自配置，全市场范围来自 point-in-time master。

### P2-DATA-05 — Parquet/DuckDB/PostgreSQL 存储

- 状态：`OPEN`
- 文件：`data/storage.py`
- 实现：实现追加式 Gold、默认最新版本视图和锁定版本读取。
- 硬约束：研究端只读；Gold 不覆盖；所有正式读取显式传 data_version。

### P2-DATA-06 — 快照生命周期

- 状态：`OPEN`
- 文件：`data/snapshot.py`
- 依赖：P2-DATA-04、P2-DATA-05
- 实现：create/pull/verify/restore、文件哈希、原子 latest 切换和安全备份。
- 验收：任一哈希不匹配时拒绝恢复，不留下半切换数据目录。

## Phase 3：研究与策略

### P3-RESEARCH-01 — Point-in-time 资产池

- 状态：`OPEN`
- 文件：`research/universe.py`
- 依赖：P2-DATA-05
- 实现：按目标日期解析上市、退市、主题成员和流动性过滤。
- 硬约束：不得用今天的成员回测历史；探索模式必须带幸存者偏差警告。

### P3-RESEARCH-02 — 公共因子

- 状态：`OPEN`
- 文件：`research/factors.py`
- 依赖：P2-DATA-05
- 实现：动量、相对强弱、均线距离、成交额比、波动率和回撤。
- 硬约束：只使用锁定版本的 qfq 序列；历史不足返回不可评估而非补零。

### P3-RESEARCH-03 — 策略运行上下文

- 状态：`OPEN`
- 文件：`research/strategy.py`
- 依赖：P2-DATA-04、P3-RESEARCH-01、P3-RESEARCH-02
- 实现：加入 snapshot_id、配置哈希、基准、日历和门禁结果；建立运行器。
- 验收：运行器先检查 required_readiness，再调用策略。

### P3-RESEARCH-04 — 策略注册与批量运行

- 状态：`OPEN`
- 文件：`research/strategy_registry.py`
- 实现：加载 enabled 策略、校验资金权重、创建单个/全部策略并输出版本清单。
- 硬约束：保持显式注册，不使用目录扫描或 import 副作用。

### P3-STRATEGY-01 — ETF 轮动策略

- 状态：`OPEN`
- 文件：`strategies/etf_rotation/strategy.py`、`config.py`
- 依赖：P3-RESEARCH-01/02/03
- 实现：完成候选池、20/60 日因子、风险开关、0~2 只选择和目标权重。
- 验收：锁定快照下输出确定；全下跌或门禁失败时不生成正式目标。

### P3-STRATEGY-02 — 主题轮动策略

- 状态：`OPEN`
- 文件：`strategies/theme_rotation/strategy.py`、`config.py`
- 实现：point-in-time 主题成员、主题评分、主题内选股和集中度约束。
- 验收：历史主题成员可追溯，不使用当前主题成员替代历史成员。

### P3-STRATEGY-03 — ETF 横截面策略

- 状态：`OPEN`
- 文件：`strategies/cross_sectional/strategy.py`、`config.py`
- 实现：全市场过滤、因子标准化、加权评分、Top-N 与流动性约束。
- 验收：仅在 CROSS_SECTION_READY 与 FEATURE_READY 同时通过时运行。

### P3-RESEARCH-05 — 多策略组合

- 状态：`OPEN`
- 文件：`research/portfolio.py`
- 依赖：P3-STRATEGY-01/02/03
- 实现：资金预算、重复标的归因、单标的上限、现金下限和 risk-off 缩放。
- 验收：最终权重不超过预算，所有调整原因可审计。

### P3-RESEARCH-06 — 事件驱动回测

- 状态：`OPEN`
- 文件：`research/backtest.py`
- 依赖：P1-MODEL-03/04/05、P3-RESEARCH-05
- 实现：T 收盘信号、T+1 raw 开盘撮合、涨跌停/停牌/手数/费用/账本/指标。
- 验收：锁定数据版本可重放，禁止 qfq 成交和未来数据泄漏。

## Phase 4：报告、AI 与 CLI

### P4-REPORT-01 — 四类报告

- 状态：`OPEN`
- 文件：`reporting/reports.py`
- 实现：daily/weekly/backtest/quality 的 JSON、Markdown、HTML 和必要图表。
- 验收：报告记录 run_id、策略版本、配置哈希、data_version 和门禁状态。

### P4-AI-01 — 只读 AI 解释

- 状态：`OPEN`
- 文件：`reporting/ai_evaluation.py`
- 依赖：P4-REPORT-01
- 实现：白名单输入、Prompt 版本、JSON Schema、成本限制、重试和归档。
- 硬约束：不得修改或建议绕过信号、候选池、门禁、订单和持仓。

### P4-CLI-01 — 数据 CLI

- 状态：`OPEN`
- 文件：`cli/data.py`
- 依赖：Phase 2
- 实现：daily/backfill/reconcile/publish/snapshot/status/compact，统一退出码。

### P4-CLI-02 — 研究 CLI

- 状态：`OPEN`
- 文件：`cli/research.py`
- 依赖：Phase 3
- 实现：run/backtest/replay/build-factors，支持单策略与全部 enabled 策略。

### P4-CLI-03 — 顶层命令装配

- 状态：`OPEN`
- 文件：`cli/main.py`
- 依赖：P4-CLI-01/02、P4-REPORT-01、P4-AI-01
- 实现：装配 scheduler/report/ai/config/migrate 命令和一致的错误输出。

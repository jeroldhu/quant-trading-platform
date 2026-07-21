# Quant Trading — 架构设计文档

> 版本: v0.1.0 | 日期: 2026-07-21 | 状态: 设计阶段

## 1. 项目概述

Quant Trading 是 `04-quantitative-tradding`(quant-theme) 和 `05-stock-arvester`(etf-pipeline)
两个项目的统一重构产物。旧项目存在代码重叠、跨仓库依赖、配置分散等问题。

重构后的项目是一个完整的 A 股量化研究系统，覆盖四个层次：

1. **数据层** — ETF/指数/股票行情采集、多源交叉验证、快照发布
2. **研究层** — 因子计算、主题轮动、横截面选股、事件回测
3. **AI 评估层** — 调用 DeepSeek 对回测结果、信号质量、数据异常进行自然语言评估
4. **执行层** — 信号生成、报告输出、模拟交易

### 1.1 核心原则

- **点-in-time 安全**。任何交易日只能使用该日及之前的数据，历史回测不允许未来信息泄露
- **多源共识**。关键数据至少两个独立来源通过校验后才进入正式门禁，单源数据标为 PROVISIONAL
- **门禁阻断**。数据质量不通过时，信号和回测不静默降级，必须显式中断
- **可复现**。所有回测输出附带完整策略配置快照和 Git commit，结果可独立验证
- **不连接券商**。所有输出仅用于量化研究和策略验证，不构成投资建议

### 1.2 技术栈

| 维度 | 选择 |
|------|------|
| 语言 | Python 3.12+ |
| 包管理 | uv + hatchling |
| CLI 框架 | Typer |
| 配置 | Pydantic Settings + YAML |
| 数据存储 | DuckDB(本地) + Parquet(分区) + PostgreSQL(远程) |
| 数据采集 | HTTP(requests/lxml) + 多源适配器 |
| 日志 | structlog |
| 验证 | mypy(strict) + ruff + CLI smoke checks |
| 容器化 | Docker + docker-compose |
| 时区 | Asia/Shanghai |

---

## 2. 项目结构

```
06-quant-trading/
├── src/quant_trading/
│   ├── __init__.py
│   ├── config.py                 # Settings + YAML 配置加载
│   ├── models/                   # 稳定领域实体，按实体拆分
│   │   ├── __init__.py
│   │   ├── bar.py                # Bar, BarAdjustment
│   │   ├── signal.py             # Signal, TargetPosition
│   │   ├── order.py              # PendingOrder, Execution
│   │   ├── trade.py              # Trade
│   │   └── position.py           # Position, PortfolioSnapshot
│   │
│   ├── data/                     # 数据采集、校验和只读访问
│   │   ├── __init__.py
│   │   ├── providers.py          # Provider Protocol + 初期数据源
│   │   ├── pipeline.py           # Raw → Bronze → Silver → Gold
│   │   ├── validation.py         # 多源对账与质量检查
│   │   ├── readiness.py          # 所有门禁的唯一实现
│   │   ├── storage.py            # Parquet/DuckDB/PostgreSQL 门面
│   │   └── snapshot.py           # 快照创建、拉取、校验
│   │
│   ├── research/                 # 研究、策略与回测
│   │   ├── __init__.py
│   │   ├── universe.py           # point-in-time 资产池
│   │   ├── factors.py            # 跨策略复用的纯因子
│   │   ├── strategy.py           # Strategy Protocol、上下文和结果
│   │   ├── strategy_registry.py  # 显式注册、创建和配置校验
│   │   ├── portfolio.py          # 多策略合并与组合约束
│   │   ├── backtest.py           # T+1 撮合、账本和指标
│   │   └── strategies/           # 一策略一包，可独立增删
│   │       ├── __init__.py       # BUILTIN_STRATEGIES 显式清单
│   │       ├── etf_rotation/
│   │       │   ├── strategy.py
│   │       │   └── config.py
│   │       ├── theme_rotation/
│   │       │   ├── strategy.py
│   │       │   └── config.py
│   │       └── cross_sectional/
│   │           ├── strategy.py
│   │           └── config.py
│   │
│   ├── reporting/                # 报告和可选 AI 解释
│   │   ├── __init__.py
│   │   ├── reports.py
│   │   └── ai_evaluation.py      # 只读，不参与信号与门禁
│   │
│   └── cli/                      # Typer CLI 与依赖装配
│       ├── __init__.py
│       ├── main.py
│       ├── data.py
│       └── research.py
│
├── configs/
│   ├── pipeline.yaml
│   ├── universes.yaml
│   └── strategies/               # 每个策略独立配置
│       ├── etf_rotation.yaml
│       ├── theme_rotation.yaml
│       └── cross_sectional.yaml
│
├── deploy/                       # 部署配置
│   ├── docker/
│   │   ├── Dockerfile
│   │   ├── docker-compose.yml
│   │   └── crontab
│   ├── postgresql/
│   │   ├── docker-compose.yml
│   │   ├── init/
│   │   └── config/
│   └── aliyun.env.example
│
├── docs/                         # 设计阶段先保持少量主题文档
│   ├── index.md
│   ├── architecture.md
│   ├── data-contract.md
│   ├── strategy-guide.md
│   ├── ai-evaluation.md
│   └── operations.md
│
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── AGENTS.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── .env.example
└── .gitignore
```

### 2.1 拆包原则

- 默认使用扁平模块，不为尚不存在的第二种实现提前创建目录。
- `providers.py`、`storage.py` 出现三个以上独立实现时再拆成包。
- `factors.py` 因子数量明显增长、需要分类维护时再拆成 `factors/`。
- `backtest.py` 出现多个撮合模型或模拟盘时，再拆出 `backtest/` 与 `execution/`。
- AI 只有在脱离报告、形成独立用例后才拆为单独层。
- 策略从第一天即一策略一包，因为策略需要独立增删、配置、版本和测试。

---

## 3. 模块分层与职责

### 3.1 分层依赖规则

```
cli → reporting → research → data → models
```

**硬规则：**
- `models/` 不依赖任何业务模块。
- `data/` 可依赖 `models/`，不得依赖 `research/`。
- `research/` 只能通过只读接口消费 Gold 数据，不直接调用外部数据源。
- `reporting/` 只消费结果对象；AI 解释不能修改门禁、信号、目标仓位或订单。
- `cli/` 是组合根，负责加载配置、创建策略和装配依赖。
- 循环依赖和为了“纯架构”而增加的空接口层都不允许。

### 3.2 各层职责

#### models — 领域模型

| 模块 | 职责 | 关键输出 |
|------|------|----------|
| `bar.py` | K 线与复权类型 | `Bar`, `BarAdjustment` |
| `signal.py` | 信号与目标仓位 | `Signal`, `TargetPosition` |
| `order.py` | 待执行订单与撮合结果 | `PendingOrder`, `Execution` |
| `trade.py` | 成交审计记录 | `Trade` |
| `position.py` | 持仓与组合快照 | `Position`, `PortfolioSnapshot` |

候选池、主题和基准属于可版本化业务配置，放在 `configs/universes.yaml`，
不写入 Python 常量。交易日历、代码规范化等逻辑在初期放在其主要调用模块中，
出现跨域复用后再提取，避免创建通用杂物模块。

#### data — 数据管道层

核心数据流：`Raw → Bronze → Silver → Gold`

```
┌───────┐    ┌────────┐    ┌────────┐    ┌──────┐
│  Raw  │ → │ Bronze │ → │ Silver │ → │ Gold │
│ 原始  │    │ 规范化 │    │ 对账后 │    │ 已验证│
│ HTTP  │    │ Parquet│    │ Parquet│    │Parquet│
└───────┘    └────────┘    └────────┘    └──────┘
 gzip 保存     字段统一       单源记录      多源共识
 请求元数据    代码标准化     冲突隔离      门禁通过
 SHA-256      主键去重       质量标记      特征因子
```

| 阶段 | 触发 | 存储 | 内容 |
|------|------|------|------|
| Raw | 每次采集 | `data/raw/` | 原始 HTTP 响应 gzip，带请求参数、抓取时间、SHA-256 |
| Bronze | Raw 写入后 | `data/bronze/` | 字段规范化、代码统一、主键去重覆盖 |
| Silver | Bronze 写入后 | `data/silver/` | 单源记录、冲突标记、质量 ISSUE 留痕 |
| Gold | 校验通过后 | `data/gold/` | 多源共识记录、PASS/PASS_OFFICIAL 状态 |

生产采集默认覆盖 point-in-time 全市场 ETF；`configs/universes.yaml` 中的候选池
只限制策略研究范围，不裁剪基础采集。因子和信号使用 `qfq` 连续价格，订单、现金、
涨跌停和成交审计使用 `raw` 真实价格。正式回测必须锁定 `data_version` 或
`snapshot_id`。完整规则见[数据契约](data-contract.md)。

**关键数据源：**

| 数据源 | 覆盖内容 | 角色 |
|--------|----------|------|
| 东方财富 | ETF 全市场快照、历史日线、净值 | 主源(行情)、单源(净值→PROVISIONAL) |
| 腾讯 | 历史日线(未复权+前复权)、5分钟线 | 校验源 |
| 同花顺 | 历史日线(前复权)、成交额 | 校验源(成交额交叉验证) |
| 通达信 | 未复权日线、除权因子、5分钟线 | 校验源(前复权因子) |
| Tushare | 股票基础信息、日历、日线、复权因子 | 可选增强源 |
| AKShare | 板块资金流向、龙虎榜 | 板块发现源 |

**来源去重规则：** 同一底层来源的不同包装只计一次。例如：AKShare/Eastmoney 与其他东方财富包装不能伪装成两个独立源。

**门禁体系：**

| 门禁 | 检查范围 | 阻断影响 |
|------|----------|----------|
| `DAILY_MARKET_READY` | 当日行情采集、双源校验、覆盖率 >= 0.99 | `publish` 失败 |
| `FEATURE_READY` | 指定窗口 qfq Gold 完整性 | 该 ETF 特征因子不可用 |
| `ROTATION_READY` | 候选 ETF raw/qfq Gold + 沪深 300 + 交易日历 | 周频 ETF 信号不生成 |
| `CROSS_SECTION_READY` | 全市场 ETF 61 日双源历史 | 全市场横截面策略不可用 |
| `STOCK_BACKTEST_READY` | 涨跌停/停复牌/ST 权限 | 股票回测不可用 |

策略在独立配置中声明所需门禁，研究运行器执行前检查。门禁间无强制前置依赖——
例如沪深 300 缺失不影响 ETF 行情发布，全市场历史回填失败不影响固定候选池轮动。

#### research — 研究策略层

**信号→成交状态机：**

```
T 日收盘
  │
  ├── 因子计算 → 信号生成
  │      │
  │      ▼
  │   Signal(T)           ← 仅包含目标证券、方向、建议权重、得分
  │      │
  │      ▼
  │   PendingOrder(T+1)   ← 开盘前创建待执行订单
  │      │                  含：ts_code, side, target_qty, limit_price, ttl
  │      │
  T+1 开盘 ──────────────→ Execution(T+1)
  │                             │
  │                    ┌────────┼────────┐
  │                    ▼        ▼        ▼
  │                Filled   Rejected  Expired
  │                (全部/  (涨跌停/  (TTL 到期
  │                部分成交) 停牌)     仍未成交)
  │                    │
  │                    ▼
  │              Position 更新
  │              Cash 扣减/释放
  │
  ▼
组合账本 → 净值曲线 → 绩效指标
```

**订单模型：**

```python
@dataclass(frozen=True)
class PendingOrder:
    """T 日收盘后创建、T+1 开盘执行的待执行订单。"""
    order_id: str
    signal_id: str          # 追溯到源 Signal
    ts_code: str
    side: Literal["BUY", "SELL"]
    target_qty: int         # 目标数量（手数取整后）
    limit_price: float      # 限价（涨跌停价为上限）
    created_at: datetime    # T 日收盘
    execute_date: date      # T+1
    ttl: int = 1            # 存活交易日数，超期 Expired
    status: str = "PENDING"

@dataclass(frozen=True)
class Execution:
    """撮合结果。"""
    order_id: str
    filled_qty: int         # 实际成交数量，可为 0
    filled_price: float     # 成交均价
    fee: float
    status: Literal["FILLED", "PARTIAL", "REJECTED", "EXPIRED"]
    reject_reason: str | None  # 如 "limit_up_block"/"suspended"/"zero_volume"
```

**撮合规则（T+1 开盘）：**

| 条件 | 结果 | 说明 |
|------|------|------|
| 正常开盘、有成交 | `FILLED` | 以未复权开盘价成交 |
| 开盘涨停 | `REJECTED`（买单）| 涨停不买，订单取消 |
| 开盘跌停 | `REJECTED`（卖单）| 跌停不卖，订单取消 |
| 停牌 | `REJECTED` | 当日无法交易 |
| 零成交 | `REJECTED` | 无成交价参考 |
| 开盘可交易但量不足 | `PARTIAL` | 成交可成交量，剩余取消 |
| TTL 到期 | `EXPIRED` | 默认 TTL=1，即 T+1 未成交则过期 |

**实时运行 vs 回测的一致性：**

- 实时运行（T 日）：只生成 Signal，不创建 PendingOrder。T+1 开盘价尚不可知
- 回测引擎（全量历史）：模拟时钟推进，在 T+1 时间点撮合 T 日的 PendingOrder
- 成交结果写入同一数据模型，实时和回测的持仓/净值可对比

**策略体系：**

每个策略使用独立包，实现一个最小 `Strategy` Protocol。系统通过显式注册表
创建策略，不扫描文件系统，也不依赖“导入模块时自动注册”的副作用。

**策略接口：**

```python
# src/quant_trading/research/strategy.py
from dataclasses import dataclass
from datetime import date
from typing import Protocol
import pandas as pd

@dataclass(frozen=True)
class StrategyContext:
    """策略执行上下文——所有策略接收相同结构的输入。"""
    trade_date: date
    bars: pd.DataFrame           # Gold qfq 日线
    benchmark: pd.DataFrame      # 基准指数 Gold 日线
    calendar: pd.DataFrame       # 交易日历
    risk_state: str              # risk_on / risk_off

class Strategy(Protocol):
    """所有正式策略都必须满足的最小协议。"""

    name: str
    version: str
    frequency: str
    required_readiness: tuple[str, ...]

    def generate_targets(
        self,
        context: StrategyContext,
    ) -> list[TargetPosition]:
        """使用当前时点可见数据生成目标仓位。"""
        ...
```

**内置策略：**

| 策略 | name | frequency | 说明 |
|------|------|-----------|------|
| 主题轮动(个股) | `theme-stock-v1` | `weekly` | 四主题内选股，2 主题 × 3 只 |
| ETF 周轮动 | `etf-weekly-v2` | `weekly` | 15 候选中选 0~2 只 |
| ETF 横截面多因子 | `etf-cross-sectional-v1` | `weekly` | 全市场/固定池，多因子排序 |
| 板块发现 | `sector-discovery-v1` | `weekly` | 概念/行业 Top 30 → Top 5 观察榜 |

**显式注册表：**

```python
# src/quant_trading/research/strategies/__init__.py
from .cross_sectional.strategy import CrossSectionalStrategy
from .etf_rotation.strategy import EtfRotationStrategy
from .theme_rotation.strategy import ThemeRotationStrategy

BUILTIN_STRATEGIES = {
    EtfRotationStrategy.name: EtfRotationStrategy,
    ThemeRotationStrategy.name: ThemeRotationStrategy,
    CrossSectionalStrategy.name: CrossSectionalStrategy,
}
```

`strategy_registry.py` 负责列出、校验和创建策略。未知名称、重复名称、配置模型
不匹配或声明的门禁不存在时必须明确报错，不能静默跳过。

```python
def create_strategy(name: str, raw_config: dict[str, object]) -> Strategy:
    try:
        strategy_type = BUILTIN_STRATEGIES[name]
    except KeyError as error:
        available = ", ".join(sorted(BUILTIN_STRATEGIES))
        raise ValueError(
            f"Unknown strategy {name!r}; available: {available}"
        ) from error
    return strategy_type.from_config(raw_config)
```

每个策略使用独立配置文件：

```yaml
# configs/strategies/etf_rotation.yaml
name: etf_rotation
enabled: true
version: "1.0.0"
capital_weight: 0.60
parameters:
  top_n: 2
  lookback_days: 20
  max_position_weight: 0.40
```

新增策略需要：策略包、配置模型、注册表条目、YAML 和可复现验证记录。删除策略时
反向移除这些内容；遗留配置必须触发校验错误。未来只有在需要安装仓库外的第三方
策略包时，才考虑 Python entry points 插件机制。

**策略协同与组合分配：**

多个策略可能同时选中同一只 ETF。`portfolio.py` 负责将
各策略的原始信号合并为最终组合权重。

```python
# src/quant_trading/research/portfolio.py

@dataclass
class AllocationResult:
    positions: dict[str, float]    # ts_code → final_weight
    attribution: dict[str, dict]   # strategy_name → {ts_code: raw_weight}
    adjustments: list[str]         # 记录调整原因

def allocate(
    strategy_signals: dict[str, list[Signal]],  # strategy_name → signals
    constraints: AllocationConstraints,
    risk_state: str,                            # "risk_on" | "risk_off"
) -> AllocationResult:
    ...
```

**组合约束（`AllocationConstraints`）：**

| 约束 | 说明 |
|------|------|
| `total_budget` | 总仓位上限（risk_on: 0.80, risk_off: 0.20） |
| `min_cash` | 最低现金比例（0.20） |
| `max_single_weight` | 单标的权重上限（ETF: 0.50, 股票: 0.20） |
| `max_concentration` | 同一跟踪指数最多入选 ETF 数 |
| `strategy_priority` | 策略优先级，冲突时高优先级策略先分配 |

**合并规则：**
- 不同策略无重复标的：直接加总
- 重复标的：按策略优先级分配（高优先级策略权重优先）
- 溢出（总权重超过 budget）：按优先级从低到高削减
- 风险关闭（risk_off）：所有信号权重按 `risk_off_multiplier` 缩放
- 输出归因记录：每只标的的最终权重可追溯到每个策略的原始建议

**实验登记（P1-7）：**

每次回测或研究运行生成唯一 `run_id`，保存完整可复现上下文：

```python
@dataclass
class RunMetadata:
    run_id: str                    # uuid
    created_at: datetime
    git_commit: str                # git rev-parse HEAD
    config_hash: str               # SHA-256 of config files
    data_version: str              # snapshot data_version
    schema_version: str
    signal_date: date
    start_date: date
    end_date: date
    benchmark: str                 # e.g. "000300.SH"
    seed: int | None
    backtest_config: dict          # fees, slippage, lot size, initial cash
    strategy_versions: dict[str, str]  # strategy_name → version
```

```bash
# 通过 run_id 重放
quant research replay --run-id abc123-def456
```

**因子体系：**

```
个股因子                      主题因子               ETF 因子
─────────────────────    ─────────────────    ─────────────────
20 日收益                   主题内平均收益        ret_20d
60 日收益                   主题内股票数          ret_60d
20 日波动率                 主题内收益离散度       relative_strength_20d
60 日最大回撤               上榜覆盖率            relative_strength_60d
20 日均成交额               净买比率              ma20_distance
MA20 偏离度                                       amount_ratio_5_20
                                                 volatility_20d
                                                 drawdown_60d
```

#### reporting — 报告层

| 报告类型 | 格式 | 内容 |
|----------|------|------|
| 每日研究 | JSON / Markdown / HTML + PNG | 当日信号、持仓、因子、净值曲线 |
| 周频轮动 | JSON / Markdown / HTML | ETF 信号、板块观察榜、门禁审计 |
| 回测绩效 | JSON / Markdown / HTML + CSV | 成交记录、绩效指标、权益曲线、归因 |
| 数据质量 | JSON / Markdown | 门禁结果、冲突记录、缺失清单 |

#### reporting 中的 AI 评估

策略回测或周频轮动完成后，自动调用 DeepSeek API 对结果做自然语言评估，
评估结果嵌入报告并独立保存 JSON。

**设计原则：**
- **只读消费**。AI 层不修改任何数据或信号，只读取回测/信号/质量报告的结果
- **Prompt 与代码分离**。评估 prompt 模板独立存放在 `prompts/` 目录，便于调优和版本管理
- **评估结果可审计**。每次评估记录：模型版本、prompt 版本、输入摘要哈希、输出原始响应
- **失败不阻断**。API 超时或出错时，报告标注"AI 评估不可用"，不阻断信号生成

**评估维度：**

| 维度 | 触发场景 | 输入 | 输出 |
|------|----------|------|------|
| 回测绩效分析 | 回测完成后 | 绩效指标、净值曲线、成交记录摘要 | 收益归因、风险诊断、基准对比解读 |
| 信号质量评估 | 回测/周频完成后 | 每期信号列表、换手率、因子暴露 | 集中度分析、因子偏移警告、衰减模式 |
| 信号解释 | 周频信号生成后 | 最新信号、因子排名、风险状态 | 规则解释、风险因素和证据摘要 |
| 数据异常解释 | 门禁失败后 | 门禁失败项、冲突记录 | 异常排查方向和可能原因 |

**数据流：**

```
回测/策略完成
      │
      ▼
┌─────────────┐   JSON 结果   ┌──────────────┐   Prompt + 结果   ┌───────────┐
│  research/  │ ────────────→ │  evaluator.py │ ────────────────→ │ DeepSeek  │
│  backtest/  │               │  (编排器)      │                   │   API     │
└─────────────┘               └──────────────┘                   └───────────┘
                                    │                                    │
                                    │ 组装上下文                          │
                                    ▼                                    ▼
                              ┌──────────────┐                   ┌─────────────┐
                              │  prompts/    │                   │ 评估 JSON   │
                              │  (模板)       │                   │ + 原始响应  │
                              └──────────────┘                   └─────────────┘
                                                                       │
                                          ┌────────────────────────────┘
                                          ▼
                                   ┌──────────────┐
                                   │  reporting/  │  ← 嵌入 Markdown/HTML 报告
                                   └──────────────┘
```

**evaluator.py 编排逻辑：**

```python
# 伪代码——实际实现时确定具体接口
class AIEvaluator:
    def evaluate_backtest(self, result: BacktestResult) -> AIEvaluation:
        """回测绩效分析"""
        context = self._build_backtest_context(result)
        prompt = render_prompt("backtest", context)
        response = self.client.chat(prompt, model="deepseek-chat")
        return AIEvaluation(dimension="backtest", raw=response, parsed=...)

    def evaluate_signals(self, signals: list[Signal], factors: DataFrame) -> AIEvaluation:
        """信号质量评估"""
        ...

    def explain_signals(self, signals: list[Signal], factors: DataFrame) -> AIEvaluation:
        """只读解释信号，不提供调仓指令"""
        ...

    def explain_anomalies(self, quality: QualityResult) -> AIEvaluation:
        """数据异常解释"""
        ...
```

**DeepSeek 客户端 (`client.py`)：**
- 封装 `openai` Python SDK（DeepSeek 兼容 OpenAI 接口格式）
- Base URL: `https://api.deepseek.com/v1`
- 模型：`deepseek-chat`（默认）/ 可通过配置切换
- 支持 `reasoning_effort` 参数控制思考深度
- 超时：120s，重试 2 次
- Token 通过 `DEEPSEEK_API_KEY` 环境变量注入

**输出结构：**

```json
{
  "evaluation_id": "eval-20260721T153000-a1b2c3d4",
  "dimension": "backtest",
  "model": "deepseek-chat",
  "created_at": "2026-07-21T15:30:00+08:00",
  "input_hash": "sha256:abc123...",
  "prompt_version": "backtest-v1",
  "content": {
    "summary": "一句话总结",
    "score": 7.5,
    "details": {
      "return_attribution": "...",
      "risk_diagnosis": "...",
      "benchmark_comparison": "..."
    },
    "warnings": ["...", "..."],
    "suggestions": ["...", "..."]
  },
  "raw_response": "DeepSeek 原始文本响应",
  "tokens": {"prompt": 2500, "completion": 800}
}
```

报告嵌入方式：
- Markdown 报告在末尾追加 `## AI 评估` 段
- HTML 报告在末尾追加 `<section class="ai-evaluation">`
- JSON 报告增加 `ai_evaluation` 字段

---

## 4. 统一 CLI 设计

### 4.1 命令总览

```bash
quant --help
```

```
Commands:
  # 数据管道
  data bootstrap           初始化目录、DuckDB 和 Parquet 分区
  data daily               执行单日采集和校验
  data backfill            回填历史日线
  data reconcile           复核目标交易日
  data publish             通过覆盖率门禁后发布
  data snapshot            创建/拉取/校验快照
  data status              查看 ETL 任务状态
  data compact             ZSTD 重写分区，小文件合并

  # 调度管理
  scheduler start          启动定时采集（Docker cron）
  scheduler stop           停止定时采集
  scheduler status         查看调度状态
  scheduler logs           查看调度日志

  # 研究
  research init-db         初始化本地 DuckDB
  research sync            同步基础数据（从快照或公开源）
  research build-factors   计算因子
  research strategies list 列出显式注册的策略
  research strategies validate 校验注册表和所有策略配置
  research run             执行一个策略或所有 enabled 策略
  research backtest        回测指定策略
  research daily-run       同步→校验→因子→信号→报告→AI 评估
  research weekly-run      ETF 周频轮动→落库→报告→AI 评估
  research validate        数据质量门禁检查

  # AI 评估
  ai evaluate backtest     对已有回测结果运行 AI 评估
  ai evaluate signals      对已有信号运行 AI 评估
  ai evaluate quality      对数据质量报告运行 AI 异常解释
  ai status                查看 API 额度、最近评估记录

  # 报告
  report daily             生成每日研究报告
  report weekly            生成周频轮动报告
  report backtest          生成回测绩效报告
  report quality           生成数据质量报告

  # 管理
  migrate                  DuckDB → PostgreSQL 迁移
  config show              显示当前配置
  config validate          校验配置文件
```

### 4.2 典型工作流

**数据工程师（阿里云服务器）：**
```bash
# 首次部署
quant data bootstrap
quant data backfill --start 2024-01-01 --end 2026-07-21

# 开启定时采集
quant scheduler start

# 手动复核
quant data reconcile --trade-date 2026-07-21
quant data publish --trade-date 2026-07-21

# 创建快照供研究端拉取
quant data snapshot --profile dev
```

**量化研究员（本地开发机）：**
```bash
# 从阿里云拉取快照
quant data snapshot pull --remote aliyun --profile dev

# 因子计算和回测
quant research build-factors --start 2024-01-01 --end 2026-07-21
quant research backtest --start 2024-01-01 --end 2026-07-21

# 每日研究
quant research daily-run --trade-date 2026-07-21
quant report daily --trade-date 2026-07-21
```

---

## 5. 配置系统

### 5.1 配置层次

```
环境变量 (.env)
    ↓ 覆盖
YAML 配置文件 (configs/*.yaml)
    ↓ 覆盖
Pydantic 模型默认值
```

### 5.2 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `QUANT_DATA_ROOT` | `./data` | 数据根目录 |
| `QUANT_SNAPSHOT_ROOT` | `./snapshots` | 快照目录 |
| `QUANT_DATABASE_URL` | — | 远程 PostgreSQL 连接串 |
| `QUANT_REMOTE_SSH_HOST` | `aliyun` | 快照同步 SSH 别名 |
| `QUANT_MARKET_MODE` | `snapshot` | `snapshot`(快照) / `live`(直接采集) |
| `QUANT_TUSHARE_TOKEN` | — | Tushare API Token |
| `QUANT_LOG_LEVEL` | `INFO` | 日志级别 |
| `QUANT_TENCENT_ENABLED` | `true` | 启用腾讯数据源 |
| `QUANT_MIN_PUBLISH_COVERAGE` | `0.99` | 发布最低覆盖率 |
| `QUANT_MIN_FREE_GB` | `10` | 最低剩余磁盘空间(GB) |
| `DEEPSEEK_API_KEY` | — | DeepSeek API Key（AI 评估） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | DeepSeek API 地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型选择 |
| `DEEPSEEK_REASONING_EFFORT` | `medium` | 思考深度：low/medium/high/xhigh |
| `QUANT_AI_ENABLED` | `true` | 启用/禁用 AI 评估 |

### 5.3 配置文件结构

`configs/pipeline.yaml`:
```yaml
data_root: data/
snapshot_root: snapshots/
timezone: Asia/Shanghai
request_timeout: 20
requests_per_second: 1.0
tencent_enabled: true
min_publish_coverage: 0.99
min_free_gb: 10
```

`configs/universes.yaml`:
```yaml
benchmark: "000300.SH"

core_themes:
  cpo:
    display_name: "CPO"
    candidates:
      - ts_code: "159508.SZ"
        name: "通信ETF"
      - ts_code: "515050.SH"
        name: "5GETF"
  semiconductor:
    display_name: "半导体"
    candidates:
      - ts_code: "512480.SH"
        name: "半导体ETF"
      - ts_code: "159516.SZ"
        name: "芯片ETF"
  storage_proxy:
    display_name: "存储"
    proxy_note: "芯片ETF代理"
    candidates:
      - ts_code: "159516.SZ"
        name: "芯片ETF"
  commercial_space:
    display_name: "商业航天"
    candidates:
      - ts_code: "512480.SH"
        name: "半导体ETF"
  military:
    display_name: "军工"
    candidates:
      - ts_code: "512660.SH"
        name: "军工ETF"
      - ts_code: "512670.SH"
        name: "国防ETF"

selection:
  min_history_bars: 61
  min_avg_amount_20d: 50000000
  max_selected: 2
  lookback_calendar_days: 240

validation:
  min_adjusted_sources: 2
  min_raw_sources: 2
  min_benchmark_sources: 2
  min_calendar_sources: 2
  min_common_returns: 60
  return_tolerance: 0.003
  price_relative_tolerance: 0.005
  amount_relative_tolerance: 0.03
  min_agreement_ratio: 0.98
```

`configs/strategies/etf_rotation.yaml`:
```yaml
name: etf_rotation
enabled: true
version: "2.0.0"
capital_weight: 0.60
required_readiness:
  - ROTATION_READY
parameters:
  benchmark: "000300.SH"
  rebalance_weekday: 4
  top_n: 2
  lookback_days: 20
  max_position_weight: 0.40
```

通用回测费用放在 `configs/backtest.yaml`，AI 配置放在
`configs/reporting.yaml`。策略专属参数不得回流到一个全局巨型 YAML。

---

## 6. 数据存储设计

### 6.1 本地存储（开发/研究端）

```
data/
├── raw/<source>/<dataset>/<fetch_date>/<run_id>/*.json.gz
├── bronze/<dataset>/year=YYYY/month=MM/part.parquet
├── silver/<dataset>/year=YYYY/month=MM/part.parquet
├── gold/<dataset>/year=YYYY/month=MM/part.parquet
├── catalog/quant.duckdb
├── staging/
└── .writer.lock
```

**DuckDB 视图：**
- `v_etf_universe_pit` — 点-in-time ETF 全市场清单
- `v_etf_daily_verified` — 已验证(Gold)日线行情
- `v_etf_features_daily` — 日频特征因子
- `v_data_readiness` — 数据就绪状态
- `v_etl_runs` — ETL 任务运行记录
- `v_quality_issues` — 质量冲突记录

### 6.2 远程存储（阿里云 PostgreSQL）

周频轮动研究结果写入远程 PostgreSQL，数据结构：

| 表 | 主键 | 内容 |
|----|------|------|
| `etf_daily_bar` | `ts_code, trade_date` | 已验证 ETF 日线 |
| `market_data_validation` | `signal_date, strategy_version, asset_code, data_kind, check_name` | 门禁通过审计 |
| `weekly_rotation_score` | `signal_date, strategy_version, entity_type, entity_code` | 周频评分 |
| `signal_daily` | `signal_date, execution_date, ts_code, strategy_version` | 信号记录 |
| `sector_flow_snapshot` | `snapshot_date, board_type, board_name` | 板块资金快照 |
| `sector_constituent_snapshot` | `snapshot_date, board_type, board_name, ts_code` | 板块成分快照 |
| `dragon_tiger_daily` | `trade_date, ts_code, reason` | 龙虎榜 |

### 6.3 快照分发

```
阿里云（唯一写入端）
  │
  ├─ 每日发布 → dev 快照（不含 Raw 响应）
  │    └─ 文件清单 + SHA-256
  │
  └─ 每周日 → full 快照（含完整 Raw 审计数据）
       └─ 文件清单 + SHA-256

研究机（只读消费端）
  │
  ├─ quant data snapshot pull --remote aliyun --profile dev
  │    └─ rsync 增量传输 → SHA-256 校验 → 原子恢复
  │
  └─ quant data snapshot verify
       └─ 校验本地快照完整性
```

---

## 7. 部署模型

### 7.1 阿里云生产服务器

```yaml
# deploy/docker/docker-compose.yml
services:
  scheduler:
    build: .
    profiles: [server]
    volumes:
      - /srv/quant-trading/data:/app/data
      - /srv/quant-trading/snapshots:/app/snapshots
      - /srv/quant-trading/logs:/app/logs
    environment:
      - QUANT_DATA_ROOT=/app/data
      - QUANT_MARKET_MODE=live

  postgresql:
    image: postgres:16
    profiles: [server]
    volumes:
      - /srv/quant-trading/postgresql:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5432:5432"

  cli:
    build: .
    profiles: [cli]
    entrypoint: ["uv", "run", "quant"]
```

**定时任务：**
```cron
# deploy/docker/crontab
30 14 * * 1-5  quant data daily --trade-date today         # 盘中快照
20 15 * * 1-5  quant data daily --trade-date today         # 收盘行情
30 18 * * 1-5  quant data daily --trade-date today         # 盘后补充
30 19 * * 1-5  quant data backfill --start today --end today  # 补齐历史
30 22 * * 1-5  quant data reconcile --trade-date today     # 晚间复核
30  7 * * 1-5  quant data reconcile --trade-date today     # 次晨复核
 0  8 * * 1-5  quant data publish --trade-date latest       # 发布
 0 23 * * 5    quant data snapshot --profile dev            # 周末快照
30 23 * * 0    quant data snapshot --profile full            # 全量快照
 0  2 1  * *   quant data compact                           # 月初合并分区
```

### 7.2 研究机（本地/开发机）

不运行定时采集。通过快照拉取获取数据，或设置 `QUANT_MARKET_MODE=live` 启用直接采集（仅开发/测试用）。

```bash
# 拉取快照
quant data snapshot pull --remote aliyun --profile dev

# 本地研究
quant research daily-run --trade-date 2026-07-21
quant research backtest --start 2024-01-01 --end 2026-07-21
```

---

## 8. 从旧项目迁移

### 8.1 迁移路径

```
04-quantitative-tradding ─┐
                           ├──→ 06-quant-tradding
05-stock-arvester ────────┘
```

### 8.2 分阶段计划

**Phase 1：最小骨架**
- [ ] 项目骨架：`pyproject.toml`、`uv sync`、目录结构
- [ ] 统一配置系统：Pydantic Settings + YAML
- [ ] `models/`：按实体迁移稳定领域模型
- [ ] 统一 CLI 骨架：Typer 命令注册
- [ ] 日志、时区、`.env.example`

**Phase 2：数据管道迁移**
- [ ] `data/providers.py`：先迁移正式链路必需的数据源
- [ ] `data/pipeline.py`：迁移采集编排器，替换 Click → Typer
- [ ] `data/storage.py`：统一 Parquet + DuckDB 访问
- [ ] `data/validation.py`：多源对账
- [ ] `data/readiness.py`：门禁体系
- [ ] `data/snapshot.py`：快照创建/拉取/校验
- [ ] Docker 部署和定时调度
- [ ] Ruff、mypy strict 与 CLI smoke 验证

**Phase 3：研究系统迁移**
- [ ] `research/universe.py`：point-in-time 股票/ETF 池
- [ ] `research/factors.py`：跨策略复用因子
- [ ] `research/strategy.py` + `strategy_registry.py`：协议与显式注册表
- [ ] `research/strategies/`：一策略一包迁移内置策略
- [ ] `research/portfolio.py` + `backtest.py`：组合、撮合与回测
- [ ] 本地 DuckDB + 远程 PostgreSQL 存储
- [ ] 可复现的策略前后对比与 CLI smoke 验证

**Phase 4：AI 评估和报告收尾**
- [ ] `reporting/reports.py`：四类报告生成
- [ ] `reporting/ai_evaluation.py`：可选、只读的三维度 AI 解释
- [ ] CLI 命令收尾和参数校验
- [ ] 全量端到端测试
- [ ] `README.md`、运维文档

**Phase 5：旧项目归档**
- [ ] 04 和 05 的 README 添加归档说明
- [ ] 确认所有功能可在 06 中复现
- [ ] 旧仓库设为只读

### 8.3 破坏性变更清单

相对于旧项目，以下行为会改变：

| 变更 | 旧行为 | 新行为 |
|------|--------|--------|
| 包名 | `quant_theme` / `etf_pipeline` | `quant_trading` |
| CLI 命令 | `quant xxx` / `etf-pipeline xxx` | `quant data xxx` / `quant research xxx` |
| 配置文件 | 分散在各自 configs/ | 统一 configs/ 但结构重新设计 |
| 环境变量 | `QUANT_*` / `ETF_*` | 统一 `QUANT_*` 前缀 |
| 构建系统 | hatchling(04) / setuptools(05) | hatchling |
| Python 版本 | 3.12(04) / 3.9+(05) | 3.12+ |
| CLI 框架 | Typer(04) / Click(05) | Typer |
| 数据目录 | `data/quant.duckdb`(04) / `data/catalog/etf.duckdb`(05) | `data/catalog/quant.duckdb` |
| 快照路径 | `data/stock-harvester/`(04) / `snapshots/`(05) | `snapshots/` |
| Docker profile | `server`(05 的 scheduler) | `server`(统一) |

---

## 9. 关键设计决策

### 9.1 为什么统一 CLI 管理调度而不是保留独立的 Docker Compose？

原 05 项目的 Docker 调度通过 `docker compose --profile server up -d scheduler` 启动，cron 写在容器内。统一 CLI 管理后：

- `quant scheduler start` 仍然启动 Docker 容器，但封装了配置校验和健康检查
- 本地开发时不会意外启动采集（需要显式 `--mode live`）
- 日志通过 `quant scheduler logs` 统一查看，不需要 `docker logs`

底层仍然是 Docker + cron，CLI 只是管理层，不替代容器运行时。

### 9.2 为什么 data 层不直接 import research 层的因子？

数据管道发布的是通用特征因子（动量、波动率、成交额），这些是数据质量的一部分，与具体策略无关。策略层的因子（主题排名、板块内评分）只依赖已验证的 Gold 数据，不影响数据发布门禁。

### 9.3 为什么保留 DuckDB + PostgreSQL 两层存储？

- DuckDB 是本地分析引擎：零配置、列式存储、适合回测扫表
- PostgreSQL 是远程持久化：周频信号需要跨机器共享、需要并发安全
- 两者不互斥：DuckDB 存储行情和因子，PostgreSQL 存储信号和门禁审计

### 9.4 为什么选 DeepSeek 而不是其他 LLM？

- **成本**。DeepSeek Chat 的定价约为 GPT-4o 的 1/10，评估类任务 token 消耗大（每次评估数千 token 上下文），成本差异显著
- **兼容性**。DeepSeek API 兼容 OpenAI 接口格式，用 `openai` SDK 设置 `base_url` 即可切换，锁定成本低
- **中文能力**。A 股研究报告需要中文输出，DeepSeek 的中文理解和生成质量在同类模型中领先
- **推理深度**。`reasoning_effort` 参数允许在需要详细诊断的回测评估中使用更高思考深度，日常信号评估使用较低深度以节省延迟

### 9.5 为什么 AI 评估失败不阻断信号生成？

AI 评估是增值分析，不是数据门禁。信号生成依赖的是经过多源验证的客观数据。
LLM 输出具有随机性（temperature > 0），不适合作为交易决策的阻断条件。
评估不可用时，报告明确标注，用户知道缺失了什么信息。

---

## 10. 附录

### A. 旧项目文件对照表

| 06 新路径 | 04 旧路径 | 05 旧路径 |
|-----------|-----------|-----------|
| `src/quant_trading/models/` | `src/quant_theme/models/` | — |
| `src/quant_trading/data/pipeline.py` | — | `src/etf_pipeline/pipeline.py` |
| `src/quant_trading/data/providers.py` | `src/quant_theme/providers/` | `src/etf_pipeline/sources/` |
| `src/quant_trading/data/storage.py` | — | `src/etf_pipeline/storage.py` |
| `src/quant_trading/data/validation.py` | — | `src/etf_pipeline/validation.py` |
| `src/quant_trading/data/readiness.py` | — | `src/etf_pipeline/readiness.py` |
| `src/quant_trading/data/snapshot.py` | `src/quant_theme/providers/aliyun_snapshot.py` | `src/etf_pipeline/data_sync.py` |
| `src/quant_trading/research/universe.py` | `src/quant_theme/universe/` | — |
| `src/quant_trading/research/factors.py` | `src/quant_theme/factors/` | — |
| `src/quant_trading/research/strategies/` | `src/quant_theme/strategy/` | — |
| `src/quant_trading/research/backtest.py` | `src/quant_theme/backtest/` | — |
| `src/quant_trading/reporting/` | `src/quant_theme/reporting/` | — |
| `src/quant_trading/reporting/ai_evaluation.py` | 全新模块 | 全新模块 |
| `src/quant_trading/cli/` | `src/quant_theme/cli.py` | `src/etf_pipeline/cli.py` |
| `configs/` | `configs/` | `.env.example` 中的默认值 |
| `deploy/docker/` | — | `Dockerfile`, `docker-compose.yml` |
| `deploy/postgresql/` | `deploy/postgresql/` | — |

### B. 术语表

| 术语 | 含义 |
|------|------|
| Gold | 通过多源共识校验的已验证数据 |
| Silver | 单源记录，含冲突标记和隔离记录 |
| Bronze | 字段规范化后的数据 |
| Raw | 原始 HTTP 响应 |
| qfq | 前复权价格 |
| 点-in-time | 使用截至该交易日已保存的数据，不回填未来信息 |
| 门禁 | 数据发布前的强制性质量检查 |
| 快照 | 某个时间点数据目录的一致副本 |
| 候选 ETF | 策略池中可选择的 ETF，共 15 只 |
| 代表 ETF | 每个主题中 20 日流动性最高的候选 ETF |

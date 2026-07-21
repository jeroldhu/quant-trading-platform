# 策略开发指南

> 版本: v1.0.0 | 日期: 2026-07-21

本文档指导如何在 Quant Trading 系统中添加、修改和调试量化策略。

---

## 1. 策略体系总览

```
┌──────────────────────────────────────────────────────┐
│                    策略层入口                          │
│              quant research daily-run                 │
│              quant research weekly-run                │
│              quant research backtest                  │
└─────────────────────┬────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
   ┌─────────┐ ┌──────────┐ ┌──────────────┐
   │主题轮动  │ │ETF 周轮动 │ │ETF 横截面    │
   │(个股)    │ │(5 主题)   │ │多因子(全市场) │
   └─────────┘ └──────────┘ └──────────────┘
        │             │             │
        └─────────────┼─────────────┘
                      ▼
              ┌──────────────┐
              │  板块发现     │
              │  (观察榜)     │
              └──────────────┘
```

策略共享少量稳定接口，每个具体策略拥有独立实现和配置：

| 组件 | 位置 | 职责 |
|------|------|------|
| 公共协议 | `research/strategy.py` | 上下文、目标仓位和 Strategy Protocol |
| 注册表 | `research/strategy_registry.py` | 列出、校验和创建策略 |
| 具体策略 | `research/strategies/<name>/` | 一策略一包，封装专属规则和配置模型 |
| 共享能力 | `research/universe.py`、`factors.py` | point-in-time 资产池与复用因子 |
| 参数 | `configs/strategies/<name>.yaml` | 独立启用、版本、资金权重和参数 |

---

## 2. 快速上手

### 2.1 跑通默认策略

```bash
# 同步数据
quant research sync

# 执行每日研究（因子→信号→回测→报告）
quant research daily-run --trade-date 2026-07-21

# 查看输出
ls reports/daily/20260721*
# 20260721.json  20260721.md  20260721.html  20260721_equity.png
```

### 2.2 回测某个参数组合

```bash
# 回测指定策略和参数文件
quant research backtest --strategy etf_rotation \
  --config configs/strategies/etf_rotation.yaml \
  --start 2024-01-01 --end 2026-07-21

# 查看回测结果
ls reports/backtest/
# daily_equity.csv  metrics.json  run_metadata.json
```

### 2.3 只计算因子，不跑回测

```bash
quant research build-factors --start 2024-01-01 --end 2026-07-21
```

---

## 3. 增加和删除策略

### 3.1 策略包结构

```text
src/quant_trading/research/strategies/momentum/
├── strategy.py
└── config.py

configs/strategies/momentum.yaml
```

`strategy.py` 实现公共 `Strategy` Protocol，`config.py` 使用 Pydantic 定义该策略
专属参数。策略不得自行访问网络或绕过 Gold 数据接口。

### 3.2 注册策略

在 `research/strategies/__init__.py` 的显式清单中加入策略：

```python
from .momentum.strategy import MomentumStrategy

BUILTIN_STRATEGIES = {
    # 已有策略...
    MomentumStrategy.name: MomentumStrategy,
}
```

不使用目录扫描、装饰器副作用或动态 import。这样可用策略清单可审查、可被 mypy
检查，也不会因漏 import 导致策略悄悄消失。

### 3.3 配置与运行

```yaml
# configs/strategies/momentum.yaml
name: momentum
enabled: true
version: "1.0.0"
capital_weight: 0.30
parameters:
  lookback_days: 252
  skip_recent_days: 21
  top_n: 10
```

```bash
# 查看仓库中可用策略
quant research strategies list

# 校验注册表和全部策略配置
quant research strategies validate

# 运行一个策略
quant research run --strategy momentum --trade-date 2026-07-21

# 运行所有 enabled=true 的策略
quant research run --all --trade-date 2026-07-21
```

### 3.4 删除策略

删除策略包、注册表条目、配置文件和对应验证记录。若其他配置仍引用已删除策略，
`strategies validate` 必须报错，运行时不得静默忽略。

---

## 4. 添加新主题

### 4.1 修改 `configs/universes.yaml`

```yaml
core_themes:
  # ... 已有主题 ...

  new_energy:                          # 新增：新能源
    display_name: "新能源"
    candidates:
      - ts_code: "159875.SZ"
        name: "新能源ETF"
      - ts_code: "516160.SH"
        name: "新能源车ETF"
```

### 4.2 保持单一配置来源

候选 ETF 和主题只在 `configs/universes.yaml` 中维护，不同步复制到 Python 常量。
策略通过已校验的配置对象读取，避免配置与代码产生两份真相。

### 4.3 验证

```bash
# 校验配置完整性
quant config validate

# 新主题回测
quant research backtest --start 2025-01-01 --end 2026-07-21
```

### 4.4 版本号递增

```yaml
# configs/universes.yaml
version: "weekly-rotation-v2.0.0"  # → "weekly-rotation-v2.1.0"
```

版本变更自动触发全量信号重算，周频信号落库时携带此版本号。

---

## 5. 添加新 ETF 候选

### 5.1 确认数据可用性

在添加候选 ETF 之前，确认 `ROTATION_READY` 需要覆盖新代码：

```bash
# 在服务器端手动拉取新 ETF 的历史数据
quant data backfill --start 2024-01-01 --end 2026-07-21 \
  --instruments "159875.SZ"
```

### 5.2 添加候选

```yaml
# configs/universes.yaml
core_themes:
  new_energy:
    candidates:
      - ts_code: "159875.SZ"
        name: "新能源ETF"
      - ts_code: "516160.SH"
        name: "新能源车ETF"
      - ts_code: "159645.SZ"       # 新增候选
        name: "绿电ETF"
```

### 5.3 验证流动性

确保新候选满足最小流动性要求：

```python
# 检查 20 日平均成交额
from quant_trading.data.readiness import get_gold_features

features = get_gold_features(["159645.SZ"], as_of="2026-07-21")
assert features["avg_amount_20d"].iloc[-1] >= 50_000_000, "流动性不足"
```

---

## 6. 调整因子权重

### 6.1 ETF 横截面因子

```yaml
# configs/strategies/cross_sectional.yaml
cross_sectional:
  factor_weights:
    ret_20d: 0.30                    # 调高短期动量权重
    ret_60d: 0.15                    # 调低长期动量权重
    relative_strength_20d: 0.15
    relative_strength_60d: 0.10
    ma20_distance: 0.10
    amount_ratio_5_20: 0.05
    volatility_20d: -0.10            # 负权 = 惩罚高波动
    drawdown_60d: -0.05
```

**约束：** 权重绝对值之和必须 > 0，每个因子名必须来自预定义集合。

### 6.2 自定义因子

```python
# src/quant_trading/research/factors.py
import pandas as pd
import numpy as np

def compute_liquidity_score(bars: pd.DataFrame) -> pd.Series:
    """
    流动性评分：20 日成交额稳定性。

    计算 20 日成交额的变异系数的倒数。
    成交额越稳定 → 得分越高。
    """
    amount = bars.pivot(index="trade_date", columns="instrument_id", values="amount")
    cv = amount.tail(20).std() / amount.tail(20).mean()
    return 1 / (cv + 1e-8)  # 避免除零

def compute_trend_strength(bars: pd.DataFrame) -> pd.Series:
    """
    趋势强度：20 日正收益天数占比 − 负收益天数占比。
    范围 [-1, 1]。
    """
    ret = bars.pivot(index="trade_date", columns="instrument_id", values="close").pct_change()
    positive_days = (ret.tail(20) > 0).sum()
    negative_days = (ret.tail(20) < 0).sum()
    return (positive_days - negative_days) / 20
```

### 6.3 在策略中使用新因子

```python
# src/quant_trading/research/strategies/cross_sectional/strategy.py
from quant_trading.research.factors.custom_factors import (
    compute_liquidity_score,
    compute_trend_strength,
)

# 在因子计算函数中添加
def build_etf_factors(bars, benchmark, as_of_date):
    factors = pd.DataFrame(index=bars["instrument_id"].unique())
    # ... 已有因子 ...
    factors["liquidity_score"] = compute_liquidity_score(bars)
    factors["trend_strength"] = compute_trend_strength(bars)
    return factors
```

```yaml
# configs/strategies/cross_sectional.yaml
cross_sectional:
  factor_weights:
    # ... 已有权重 ...
    liquidity_score: 0.05
    trend_strength: 0.10
```

### 6.4 因子回测对比

```bash
# 旧权重回测
quant research backtest --start 2024-01-01 --end 2026-06-30 \
  --config configs/strategy_baseline.yaml

# 新权重回测
quant research backtest --start 2024-01-01 --end 2026-06-30 \
  --config configs/strategy_new_weights.yaml

# 对比两个回测结果
diff reports/backtest/baseline/metrics.json reports/backtest/new_weights/metrics.json
```

---

## 7. 回测参数调整

### 7.1 修改交易约束

```yaml
# configs/strategies/etf_rotation.yaml
backtest:
  initial_cash: 2000000              # 初始资金翻倍
  commission_rate: 0.0002            # 佣金降到万二
  minimum_commission: 5
  stock_sell_stamp_duty_rate: 0.0005 # 印花税
  slippage_rate: 0.001               # 滑点升到 0.1%
  lot_size: 100
```

### 7.2 修改仓位限制

```yaml
position:
  max_stock_weight: 0.15             # 单只股票上限 15%
  max_theme_weight: 0.40             # 单主题上限 40%
  max_total_weight: 0.90             # 总仓位上限 90%
  min_cash_weight: 0.10              # 最低现金 10%
```

### 7.3 修改风险开关

```yaml
risk:
  benchmark_ma_window: 40            # 从 60 日改为 40 日
  benchmark_return_window: 10        # 从 20 日改为 10 日
  risk_off_max_weight: 0.10          # risk_off 时最多 10% 仓位
  risk_on_max_weight: 0.90           # risk_on 时最多 90% 仓位
```

**风险开关逻辑：** 当沪深 300 收盘价低于 MA(N)，或 N 日收益 < 0 时，判定为 `risk_off`。
此时降低仓位上限、减少候选数。

### 7.4 压力测试

```bash
# 参数敏感性：滑点从 0 到 0.5%
for slip in 0 0.0005 0.001 0.002 0.005; do
  quant research backtest --start 2024-01-01 --end 2026-07-21 \
    --set backtest.slippage_rate=$slip
done

# 子区间测试：分段回测看稳定性
for year in 2022 2023 2024 2025; do
  quant research backtest \
    --start $year-01-01 --end $year-12-31
done
```

---

## 8. 板块发现策略调参

### 8.1 基础评分权重

```yaml
discovery:
  base_weights:
    return_20d: 0.40                 # 调高中期动量
    return_5d: 0.20                  # 调低短期动量
    net_flow_20d_per_company: 0.25
    net_flow_5d_per_company: 0.15
```

### 8.2 龙虎榜权重

```yaml
discovery:
  lhb_weights:
    coverage: 0.60                   # 上榜覆盖率
    net_buy_ratio: 0.40             # 净买比率
```

### 8.3 最终融合权重

```yaml
discovery:
  final_weights:
    base: 0.80                       # 基础评分占 80%
    lhb: 0.20                        # 龙虎榜占 20%
```

### 8.4 排除特定板块

```yaml
discovery:
  excluded_aliases:
    - "ST板块"
    - "退市整理"
    - "新股与次新股"
```

---

## 9. 调试技巧

### 9.1 单步调试信号

```python
# 在 Python REPL 中逐步调试
from quant_trading.research.strategy.etf_rotation import generate_weekly_signals
from quant_trading.data.readiness import get_gold_bars

bars = get_gold_bars(
    instruments=["512480.SH", "159516.SZ"],
    start="2026-06-01",
    end="2026-07-21",
    adjustment="qfq",
)

# 逐步看因子和信号
from quant_trading.research.factors.etf_factors import build_etf_factors
factors = build_etf_factors(bars, None, "2026-07-21")
print(factors.to_string())

signals = generate_weekly_signals(factors, "2026-07-21")
for s in signals:
    print(f"{s.instrument_id}: score={s.score:.3f}, weight={s.weight:.2%}")
```

### 9.2 检查哪些候选被过滤

```python
from quant_trading.research.universe.filters import filter_etf_universe

universe = filter_etf_universe(bars, features, as_of="2026-07-21")
print("通过过滤:", universe[universe["passed"]]["instrument_id"].tolist())
print("被过滤:", universe[~universe["passed"]][["instrument_id", "filter_reason"]])
```

### 9.3 手工验证回测成交

```python
from quant_trading.research.backtest.engine import run_event_backtest

result = run_event_backtest(bars, constraints, signals, config.backtest)
# 查看逐笔成交
for trade in result.trades:
    print(
        f"{trade.trade_date} {trade.ts_code} "
        f"{trade.side} {trade.qty}@{trade.price:.2f} "
        f"fee={trade.fee:.2f}"
    )
```

### 9.4 因子暴露检查

```python
# 检查信号是否过度集中在某个因子
import pandas as pd

signals_df = pd.DataFrame([s.model_dump() for s in signals])
factor_cols = [c for c in factors.columns if c != "instrument_id"]
correlations = factors[factor_cols].corrwith(signals_df.set_index("instrument_id")["score"])
print("因子与最终得分的相关性：")
print(correlations.sort_values(ascending=False))
```

---

## 10. 验证策略变更

### 10.1 固定输入验证

使用同一个 `snapshot_id`、`data_version` 和策略配置运行两次，输出的信号、
目标仓位与报告哈希必须一致。验证过程不得调用实时网络数据。

```bash
uv run ruff check .
uv run mypy src/quant_trading
uv run quant research strategies validate
```

### 10.2 策略前后对比

策略参数或逻辑变化时，使用锁定快照分别运行新旧版本，保存收益、回撤、换手率、
成交数量和信号重叠率。没有可复现对比结果的策略变更不能进入正式研究流程。

---

## 11. 策略版本管理

### 11.1 版本号规则

```
<策略名>-v<主版本>.<次版本>.<修订版本>

主版本：信号逻辑或因子定义有破坏性变更
次版本：新增候选/主题/因子（向后兼容）
修订版本：参数微调（不影响信号结构）
```

### 11.2 版本变更流程

1. 修改配置文件中的 `version`
2. 运行回测对比新旧版本
3. 如果信号发生显著变化（>20% 重叠变化），做样本外验证
4. 提交时在 commit message 中注明版本变更和影响

```bash
git commit -m "strategy(etf-weekly): v2.0.0 → v2.1.0, add new_energy theme

- 新增新能源主题，2 只候选 ETF
- 回测 2024-01-01 ~ 2026-07-21:
  - 旧版本: CAGR=12.3%, Sharpe=1.15, maxDD=-8.2%
  - 新版本: CAGR=11.8%, Sharpe=1.08, maxDD=-9.1%
- 新版本略低但分散化提升，信号重叠率 78%"
```

---

## 12. 数据读取检查清单

提交或回测策略前逐项确认：

- 候选池来自 `configs/universes.yaml` 的 point-in-time 配置，不使用今天的成员
  回测历史。
- 基础数据采集范围默认是全市场 ETF；策略候选池不能反向裁剪生产采集范围。
- 因子、动量、均线和信号明确读取 `adjustment="qfq"`。
- 订单、成交、现金、手数和涨跌停明确读取 `adjustment="raw"`。
- 代码中不存在隐式复权默认值，禁止使用 `qfq` 价格模拟成交。
- 正式回测明确传入 `data_version` 或 `snapshot_id`，结果元数据保存相同版本。
- CLI `--instruments` 只用于单次补数或排障，不应被当作永久策略配置。

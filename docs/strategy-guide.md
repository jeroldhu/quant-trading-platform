# 策略开发指南

> 版本: v1.1.0 | 日期: 2026-07-22

策略采用“一策略一包 + 一份 YAML + 显式注册表”。新增或删除策略不需要修改
数据管道，也不会依赖目录扫描或 import 副作用。

## 1. 运行策略

研究命令只读已经验证并恢复的快照，不会在本机隐式拉取行情：

```bash
uv run quant data snapshot pull --remote aliyun --profile dev
uv run quant research strategies list
uv run quant research strategies validate

# 运行所有 enabled 策略
uv run quant research run --trade-date 2026-07-21 \
  --data-version <VERSION> --output reports/research.json

# 只运行一个策略；disabled 也可被显式选择
uv run quant research run --trade-date 2026-07-21 \
  --data-version <VERSION> --strategy etf_rotation

# 在同一 snapshot/data_version/config 上检查确定性
uv run quant research replay --run-file reports/research.json
```

任一 `required_readiness` 未通过时，正式策略会明确阻断。零信号是合法结果，不能
为了产生买入建议而降低门禁或给缺失因子补零。

## 2. 历史回测

```bash
uv run quant research backtest --strategy etf_rotation \
  --start 2024-01-01 --end 2026-07-21 --data-version <VERSION> \
  --backtest-config configs/backtest.yaml --output reports/backtest.json

uv run quant report backtest --input reports/backtest.json \
  --output-dir reports/backtest
```

历史模式逐个调仓日重新解析 point-in-time 资产池并计算门禁，不会拿一次当前信号
套用完整历史。因子使用 qfq，T 日收盘产生目标，T+1 使用 raw 开盘撮合。手续费、
最低佣金、股票卖出印花税、滑点、手数、成交量上限、涨跌停和停牌均由
`configs/backtest.yaml` 明确配置或处理。

公共因子也可单独检查：

```bash
uv run quant research build-factors --trade-date 2026-07-21 \
  --data-version <VERSION> --instruments 512480.SH,159516.SZ
```

## 3. 内置策略

| 配置名 | 资产 | 频率 | 所需门禁 |
|--------|------|------|----------|
| `etf_rotation` | ETF | weekly | `ROTATION_READY`, `FEATURE_READY` |
| `cross_sectional` | ETF | weekly | `CROSS_SECTION_READY`, `FEATURE_READY` |
| `theme_rotation` | 股票 | weekly | `STOCK_BACKTEST_READY` |

主题策略必须先存在目标日期之前最近一份 `sector_constituent_snapshot`，并读取
`stock_daily_bar`，不会从 ETF Gold 或今天的板块成员降级替代：

```bash
# CSV 必须包含 board_type,board_name,ts_code
uv run quant data sector-import --input sectors.csv \
  --snapshot-date 2026-07-21 --source-id akshare_eastmoney

QUANT_TUSHARE_TOKEN=*** uv run quant data stock-backfill \
  --instruments 000001.SZ,600000.SH --start 2024-01-01 --end latest
```

`snapshot_date` 必须是真实采集或上游发布日期。AKShare 板块接口无历史参数时，
不得把当天结果回填为过去日期。

## 4. 新增策略

目录结构：

```text
src/quant_trading/research/strategies/momentum/
├── __init__.py
├── config.py
└── strategy.py
configs/strategies/momentum.yaml
```

实现类需满足 `Strategy` 协议：

```python
class MomentumStrategy:
    name = "momentum"
    version = "1.0.0"
    frequency = "weekly"
    asset_type = "etf"
    required_readiness = ("FEATURE_READY",)

    def generate_targets(
        self, context: StrategyContext
    ) -> list[TargetPosition]:
        ...
```

然后在 `research/strategies/__init__.py` 的 `BUILTIN_STRATEGIES` 中显式加入
`name → from_config`。配置文件根节点固定为：

```yaml
name: momentum
enabled: false
version: "1.0.0"
capital_weight: 0.10
required_readiness: [FEATURE_READY]
parameters:
  lookback_days: 60
```

再在 `configs/universes.yaml` 增加同名资产池。支持三种模式：

- `configured`：显式代码列表；
- `full_market`：按目标日期读取 ETF master；
- `theme_snapshot`：按目标日期读取最近的历史板块成分快照。

最后执行：

```bash
uv run quant research strategies validate
uv run ruff check .
uv run mypy src/quant_trading
```

## 5. 删除或停用策略

临时停用只需把 YAML 的 `enabled` 改为 `false`；仍可通过 `--strategy` 显式运行。
彻底删除时同时移除策略包、YAML、资产池配置和显式注册项。不要留下自动扫描或
导入即注册逻辑。

## 6. 版本与比较规则

改变因子公式、选股规则、风险开关或目标权重时必须提升策略 `version`。调整费用、
撮合规则或数据口径时还要同步更新架构与数据契约。策略变更提交应保存相同
`snapshot_id`、`data_version` 和区间下的前后回测报告；至少比较收益、回撤、波动、
换手、成交数和入选标的重叠率。

正式结果必须保留：

- `snapshot_id` 与逻辑 `data_version`；
- 策略版本、配置哈希和 Git commit；
- 每个 required readiness 的状态；
- 信号日、T+1 执行日、raw 成交价与费用明细。

"""V2 ETF 轮动策略回测入口。

用法:
    uv run python scripts/backtest_v2.py

可选参数:
    --start 2015-01-01 --end 2026-07-23 --output results.json
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quant_trading.data.storage import ParquetDuckDBStore
from quant_trading.models import BarAdjustment
from quant_trading.research.strategies.etf_rotation.v2_config import V2StrategyConfig, DEFAULT_CONFIG
from quant_trading.research.strategies.etf_rotation.v2_strategy import (
    V2EtfRotationEngine,
    ETFBar,
    DrawdownZone,
    MarketRegime,
)


def load_bars_from_store(
    store: ParquetDuckDBStore,
    instruments: tuple[str, ...],
    start: date,
    end: date,
    data_version: str,
) -> list[ETFBar]:
    """从 ParquetDuckDBStore 加载 qfq 日线数据，转为 ETFBar 列表。"""
    raw_bars = store.get_bars(
        instruments,
        start - timedelta(days=365),  # 多取一年用于指标预热
        end + timedelta(days=15),
        adjustment=BarAdjustment.QFQ,
        data_version=data_version,
    )
    # 去重
    seen = set()
    result = []
    for bar in raw_bars:
        key = (bar.instrument_id, bar.trade_date)
        if key in seen:
            continue
        seen.add(key)
        result.append(ETFBar(
            instrument_id=bar.instrument_id,
            trade_date=bar.trade_date,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            amount=bar.amount or 0.0,
        ))
    return result


def format_table(headers: list[str], rows: list[list[str]], col_widths: list[int] | None = None) -> str:
    """简单 ASCII 表格。"""
    if col_widths is None:
        col_widths = [max(len(str(row[i])) for row in [headers] + rows) + 2 for i in range(len(headers))]

    sep = "+" + "+".join("-" * w for w in col_widths) + "+"
    header_row = "|" + "|".join(h.center(col_widths[i]) for i, h in enumerate(headers)) + "|"
    data_rows = "\n".join(
        "|" + "|".join(f" {str(r[i]):<{col_widths[i] - 1}}" for i in range(len(headers))) + "|"
        for r in rows
    )
    return f"{sep}\n{header_row}\n{sep}\n{data_rows}\n{sep}"


def main():
    store = ParquetDuckDBStore(Path("data"))
    data_version = "backfill-20260724-8a0862b02c92"

    # 候选池
    candidates = {
        "510300.SH": "沪深300",
        "510500.SH": "中证500",
        "512100.SH": "中证1000",
        "159915.SZ": "创业板",
        "588000.SH": "科创50",
        "512480.SH": "半导体",
        "159516.SZ": "通信",
        "159508.SZ": "软件",
        "562500.SH": "机器人",
        "516110.SH": "电力设备",
        "512660.SH": "军工",
        "512880.SH": "证券",
        "512400.SH": "有色",
        "510880.SH": "红利",
        "518880.SH": "黄金",
    }

    benchmark_id = "510300.SH"

    start_date = date(2019, 1, 1)
    end_date = date(2026, 7, 23)

    print("=" * 60)
    print("V2 ETF 行业轮动策略回测")
    print(f"回测区间: {start_date} ~ {end_date}")
    print("=" * 60)

    # 加载数据
    print("\n[1/3] 加载行情数据...")
    all_instruments = tuple(candidates.keys())
    etf_bars = load_bars_from_store(store, all_instruments, start_date, end_date, data_version)
    bench_bars = load_bars_from_store(store, (benchmark_id,), start_date, end_date, data_version)
    print(f"  ETF 日线: {len(etf_bars):,} 条")
    print(f"  基准日线: {len(bench_bars):,} 条")

    # 初始化引擎
    print("\n[2/3] 运行回测...")
    engine = V2EtfRotationEngine(
        instruments=all_instruments,
        benchmark_id=benchmark_id,
        config=DEFAULT_CONFIG,
    )
    engine.load_data(etf_bars, bench_bars)
    result = engine.run()

    metrics = result.metrics
    print(f"  交易日数: {len(result.snapshots):,}")
    print(f"  交易笔数: {int(metrics.get('total_trades', 0))}")

    # 打印结果
    print("\n[3/3] 回测结果")
    print("=" * 60)

    # 绩效表
    perf_headers = ["指标", "V2 策略", "说明"]
    perf_rows = [
        ["累计收益率", f"{metrics.get('total_return', 0) * 100:.2f}%", "总收益 / 初始资金 - 1"],
        ["年化收益率", f"{metrics.get('annual_return', 0) * 100:.2f}%", "几何年化"],
        ["年化波动率", f"{metrics.get('annual_volatility', 0) * 100:.2f}%", "日收益标准差 × √252"],
        ["夏普比率", f"{metrics.get('sharpe_ratio', 0):.2f}", "风险调整后收益"],
        ["索提诺比率", f"{metrics.get('sortino_ratio', 0):.2f}", "只看下行风险"],
        ["最大回撤", f"{metrics.get('max_drawdown', 0) * 100:.2f}%", "峰值到谷底最大跌幅"],
        ["卡玛比率", f"{metrics.get('calmar_ratio', 0):.2f}", "年化收益 / |最大回撤|"],
        ["胜率", f"{metrics.get('win_rate', 0) * 100:.1f}%", "盈利交易 / 总交易"],
        ["总交易数", f"{int(metrics.get('total_trades', 0))}", ""],
        ["年均换手", f"{metrics.get('turnover', 0):.2f}", "总成交额 / 平均净值"],
    ]
    print(format_table(perf_headers, perf_rows))

    # 年度收益
    if result.annual_returns:
        print("\n年度收益:")
        yr_headers = ["年份", "收益率"]
        yr_rows = [[str(k), f"{v * 100:.2f}%"] for k, v in sorted(result.annual_returns.items())]
        print(format_table(yr_headers, yr_rows))

    # 风控触发统计
    snapshots = result.snapshots
    bull_days = sum(1 for s in snapshots if s.market_regime == MarketRegime.BULL)
    weak_days = sum(1 for s in snapshots if s.market_regime == MarketRegime.WEAK)
    bear_days = sum(1 for s in snapshots if s.market_regime == MarketRegime.BEAR)
    normal_days = sum(1 for s in snapshots if s.drawdown_zone == DrawdownZone.NORMAL)
    warning_days = sum(1 for s in snapshots if s.drawdown_zone == DrawdownZone.WARNING)
    severe_days = sum(1 for s in snapshots if s.drawdown_zone == DrawdownZone.SEVERE)
    cooldown_days = sum(1 for s in snapshots if s.cooldown_remaining > 0)
    empty_days = sum(1 for s in snapshots if len(s.positions) == 0)
    holding_days = len(snapshots) - empty_days

    print("\n风控统计:")
    risk_headers = ["类别", "天数", "占比"]
    risk_rows = [
        ["牛市环境 (Bull)", str(bull_days), f"{bull_days/len(snapshots)*100:.1f}%"],
        ["弱市环境 (Weak)", str(weak_days), f"{weak_days/len(snapshots)*100:.1f}%"],
        ["熊市环境 (Bear)", str(bear_days), f"{bear_days/len(snapshots)*100:.1f}%"],
        ["正常回撤区", str(normal_days), f"{normal_days/len(snapshots)*100:.1f}%"],
        ["回撤警告 (>=8%)", str(warning_days), f"{warning_days/len(snapshots)*100:.1f}%"],
        ["严重回撤 (>=12%)", str(severe_days), f"{severe_days/len(snapshots)*100:.1f}%"],
        ["冷静期", str(cooldown_days), f"{cooldown_days/len(snapshots)*100:.1f}%"],
        ["持仓天数", str(holding_days), f"{holding_days/len(snapshots)*100:.1f}%"],
        ["空仓天数", str(empty_days), f"{empty_days/len(snapshots)*100:.1f}%"],
    ]
    print(format_table(risk_headers, risk_rows))

    # 最新状态
    if snapshots:
        last = snapshots[-1]
        print(f"\n最新状态 ({last.trade_date}):")
        print(f"  净值: ¥{last.net_value:,.2f}")
        print(f"  现金: ¥{last.cash:,.2f}")
        print(f"  回撤: {last.portfolio_drawdown*100:.2f}%")
        print(f"  市场: {last.market_regime.value}")
        print(f"  目标仓位: {last.final_total_weight*100:.0f}%")
        if last.positions:
            for inst_id, pos in last.positions.items():
                name = candidates.get(inst_id, inst_id)
                print(f"  持仓: {name} × {pos.quantity} 股 (持有 {pos.holding_days} 天)")
        else:
            print("  持仓: 空仓")

    # 保存结果
    output_path = Path("data/v2_backtest_result.json")
    output_data = {
        "config": DEFAULT_CONFIG.model_dump(mode="json"),
        "metrics": {k: v for k, v in metrics.items() if isinstance(v, (int, float))},
        "annual_returns": {str(k): v for k, v in result.annual_returns.items()},
        "monthly_returns": result.monthly_returns,
        "snapshots_count": len(result.snapshots),
        "trades_count": len(result.trades),
    }
    output_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2, default=str))
    print(f"\n结果已保存至: {output_path}")


if __name__ == "__main__":
    main()

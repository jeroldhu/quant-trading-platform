"""事件驱动回测引擎。

T 日收盘信号 → T+1 raw 开盘撮合 → 涨跌停/停牌/手数/费用 → 组合账本 → 绩效指标。
禁止 qfq 成交和未来数据泄漏。
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from quant_trading.config import BacktestConfig
from quant_trading.models import (
    BarAdjustment,
    Execution,
    ExecutionStatus,
    MarketBar,
    OrderSide,
    PendingOrder,
    PortfolioSnapshot,
    Position,
    RejectReason,
    Signal,
    TargetPosition,
    Trade,
)


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: tuple[Trade, ...] = ()
    executions: tuple[Execution, ...] = ()
    snapshots: tuple[PortfolioSnapshot, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# T+1 撮合
# ---------------------------------------------------------------------------


def _next_trade_date(
    bars_by_date: dict[date, dict[str, MarketBar]],
    current: date,
) -> date | None:
    dates = sorted(d for d in bars_by_date if d > current)
    return dates[0] if dates else None


def execute_order(
    order: PendingOrder,
    bar: MarketBar,
    backtest_cfg: BacktestConfig,
    current_cash: float,
    positions: dict[str, Position],
) -> tuple[Execution, Trade | None, float, dict[str, Position]]:
    """撮合单个订单：T+1 raw 开盘价，支持涨跌停/停牌/手数/费用。"""

    if bar.instrument_id != order.instrument_id:
        raise ValueError("订单与行情标的不一致")
    if bar.trade_date != order.execution_date:
        raise ValueError("订单执行日与行情日期不一致")
    if bar.adjustment is not BarAdjustment.RAW:
        raise ValueError("成交行情必须是 raw")

    price = bar.open
    reject: str | None = None

    # 停牌/涨跌停阻断
    if bar.trading_status.value == "SUSPENDED":
        reject = RejectReason.SUSPENDED.value
    elif order.side is OrderSide.BUY and bar.trading_status.value == "LIMIT_UP":
        reject = RejectReason.LIMIT_UP_BLOCK.value
    elif order.side is OrderSide.SELL and bar.trading_status.value == "LIMIT_DOWN":
        reject = RejectReason.LIMIT_DOWN_BLOCK.value
    elif bar.volume == 0:
        reject = RejectReason.ZERO_VOLUME.value
    elif (
        order.limit_price is not None
        and order.side is OrderSide.BUY
        and price > order.limit_price
    ) or (
        order.limit_price is not None
        and order.side is OrderSide.SELL
        and price < order.limit_price
    ):
        reject = RejectReason.LIMIT_PRICE.value

    if reject:
        exec_ = Execution(
            order_id=order.order_id,
            status=ExecutionStatus.REJECTED,
            filled_quantity=0,
            remaining_quantity=order.target_quantity,
            filled_price=0.0,
            fee=0.0,
            reject_reason=reject,
        )
        return exec_, None, current_cash, dict(positions)

    # 手数取整
    lots = order.target_quantity // backtest_cfg.lot_size
    requested_qty = max(lots * backtest_cfg.lot_size, 0)
    liquidity_qty = (
        int(bar.volume * backtest_cfg.max_volume_participation / backtest_cfg.lot_size)
        * backtest_cfg.lot_size
    )
    qty = min(requested_qty, liquidity_qty)
    if qty == 0:
        exec_ = Execution(
            order_id=order.order_id,
            status=ExecutionStatus.REJECTED,
            filled_quantity=0,
            remaining_quantity=order.target_quantity,
            filled_price=0.0,
            fee=0.0,
            reject_reason=(
                RejectReason.INSUFFICIENT_LIQUIDITY.value
                if requested_qty > 0
                else "lot_size_too_small"
            ),
        )
        return exec_, None, current_cash, dict(positions)

    # 费用
    gross = qty * price
    commission = gross * backtest_cfg.commission_rate
    minimum_commission = max(backtest_cfg.minimum_commission - commission, 0.0)
    stamp_duty = (
        gross * backtest_cfg.stock_sell_stamp_duty_rate
        if order.side is OrderSide.SELL and order.asset_type == "stock"
        else 0.0
    )
    slip = gross * backtest_cfg.slippage_rate
    total_fee = commission + minimum_commission + stamp_duty + slip

    # 现金检查
    if order.side is OrderSide.BUY and gross + total_fee > current_cash:
        reject = RejectReason.INSUFFICIENT_CASH.value
        exec_ = Execution(
            order_id=order.order_id,
            status=ExecutionStatus.REJECTED,
            filled_quantity=0,
            remaining_quantity=order.target_quantity,
            filled_price=0.0,
            fee=0.0,
            reject_reason=reject,
        )
        return exec_, None, current_cash, dict(positions)

    # 持仓检查
    if order.side is OrderSide.SELL:
        pos = positions.get(order.instrument_id)
        if pos is None or pos.available_quantity < qty:
            reject = RejectReason.INSUFFICIENT_POSITION.value
            exec_ = Execution(
                order_id=order.order_id,
                status=ExecutionStatus.REJECTED,
                filled_quantity=0,
                remaining_quantity=0,
                filled_price=0.0,
                fee=0.0,
                reject_reason=reject,
            )
            return exec_, None, current_cash, dict(positions)

    # 成交
    remaining_quantity = order.target_quantity - qty
    exec_ = Execution(
        order_id=order.order_id,
        status=(
            ExecutionStatus.PARTIAL
            if remaining_quantity > 0
            else ExecutionStatus.FILLED
        ),
        filled_quantity=qty,
        remaining_quantity=remaining_quantity,
        filled_price=price,
        fee=total_fee,
    )

    trade = Trade(
        trade_id=f"T-{order.order_id}",
        order_id=order.order_id,
        instrument_id=order.instrument_id,
        side=order.side,
        quantity=qty,
        raw_price=price,
        commission=commission,
        minimum_commission=minimum_commission,
        stamp_duty=stamp_duty,
        slippage=slip,
        total_fee=total_fee,
        executed_at=datetime.combine(order.execution_date, datetime.min.time()),
    )

    new_positions = dict(positions)
    if order.side is OrderSide.BUY:
        new_cash = current_cash - gross - total_fee
        if order.instrument_id in new_positions:
            old = new_positions[order.instrument_id]
            old_total = old.available_quantity + old.frozen_quantity
            total_qty = old_total + qty
            avg_cost = (old.average_raw_cost * old_total + gross) / total_qty
            new_positions[order.instrument_id] = Position(
                instrument_id=order.instrument_id,
                available_quantity=old.available_quantity,
                frozen_quantity=old.frozen_quantity + qty,
                average_raw_cost=avg_cost,
                market_value=total_qty * price,
            )
        else:
            new_positions[order.instrument_id] = Position(
                instrument_id=order.instrument_id,
                available_quantity=0,
                frozen_quantity=qty,
                average_raw_cost=gross / qty,
                market_value=gross,
            )
    else:
        new_cash = current_cash + gross - total_fee
        old = new_positions[order.instrument_id]
        remaining = old.available_quantity - qty
        if remaining + old.frozen_quantity > 0:
            new_positions[order.instrument_id] = Position(
                instrument_id=order.instrument_id,
                available_quantity=remaining,
                frozen_quantity=old.frozen_quantity,
                average_raw_cost=old.average_raw_cost,
                market_value=(remaining + old.frozen_quantity) * price,
            )
        else:
            new_positions.pop(order.instrument_id, None)

    return exec_, trade, new_cash, new_positions


# ---------------------------------------------------------------------------
# 回测主循环
# ---------------------------------------------------------------------------


def run_backtest(
    signals: list[Signal],
    target_positions: list[TargetPosition],
    bars: list[MarketBar],
    config: BacktestConfig,
    *,
    data_version: str,
    benchmark_bars: list[MarketBar] | None = None,
) -> BacktestResult:
    """按交易日推进，T 日信号只在 T+1 raw 开盘执行并按收盘价记账。"""

    if config.execution_price_adjustment != "raw":
        raise ValueError("成交必须使用 raw 价格")
    if not data_version:
        raise ValueError("正式回测必须显式锁定 data_version")
    if any(signal.data_version != data_version for signal in signals):
        raise ValueError("信号 data_version 与回测锁定版本不一致")
    if any(target.data_version != data_version for target in target_positions):
        raise ValueError("目标仓位 data_version 与回测锁定版本不一致")

    # 索引：每个交易日每只证券的 raw K 线
    raw_bars: dict[date, dict[str, MarketBar]] = {}
    for bar in bars:
        if bar.data_version != data_version:
            raise ValueError("行情包含未锁定版本")
        if bar.adjustment is not BarAdjustment.RAW:
            continue
        raw_bars.setdefault(bar.trade_date, {})[bar.instrument_id] = bar
    if not raw_bars:
        raise ValueError("没有可用于成交的 raw 行情")

    by_execution_date: dict[date, list[Signal]] = {}
    for signal in signals:
        expected = _next_trade_date(raw_bars, signal.signal_date)
        if expected is None or signal.execution_date != expected:
            raise ValueError(
                f"信号 {signal.signal_id} 执行日不是 T+1 交易日: "
                f"expected={expected}, actual={signal.execution_date}"
            )
        by_execution_date.setdefault(signal.execution_date, []).append(signal)
    targets = {
        (
            target.signal_date,
            target.strategy_name,
            target.instrument_id,
        ): target
        for target in target_positions
    }

    cash = config.initial_cash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    executions: list[Execution] = []
    snapshots: list[PortfolioSnapshot] = []

    gross_turnover = 0.0
    for exec_date in sorted(raw_bars):
        exec_bars = raw_bars[exec_date]

        # 上一交易日买入的数量在新交易日开盘前转为可卖。
        positions = {
            instrument_id: position.model_copy(
                update={
                    "available_quantity": (
                        position.available_quantity + position.frozen_quantity
                    ),
                    "frozen_quantity": 0,
                }
            )
            for instrument_id, position in positions.items()
        }

        # 开盘前按当日 open 重新估值，避免沿用建仓日市值进行目标仓位计算。
        marked_open: dict[str, Position] = {}
        for instrument_id, position in positions.items():
            mark = exec_bars.get(instrument_id)
            market_value = (
                (position.available_quantity + position.frozen_quantity) * mark.open
                if mark is not None
                else position.market_value
            )
            marked_open[instrument_id] = position.model_copy(
                update={"market_value": market_value}
            )
        positions = marked_open

        for sig in by_execution_date.get(exec_date, []):
            target = targets.get(
                (sig.signal_date, sig.strategy_name, sig.instrument_id)
            )
            if target is None:
                raise ValueError(f"信号 {sig.signal_id} 缺少对应 TargetPosition")

            exec_bar = exec_bars.get(sig.instrument_id)
            if exec_bar is None:
                executions.append(
                    Execution(
                        order_id=f"O-{sig.signal_id}",
                        status=ExecutionStatus.REJECTED,
                        filled_quantity=0,
                        remaining_quantity=0,
                        filled_price=0.0,
                        fee=0.0,
                        reject_reason="missing_execution_bar",
                    )
                )
                continue

            portfolio_value = cash + sum(
                position.market_value for position in positions.values()
            )
            target_value = portfolio_value * target.target_weight
            target_qty = (
                int(target_value / exec_bar.open / config.lot_size) * config.lot_size
            )
            current_position = positions.get(sig.instrument_id)
            current_qty = (
                current_position.available_quantity + current_position.frozen_quantity
                if current_position is not None
                else 0
            )
            delta = target_qty - current_qty
            if delta > 0:
                side = OrderSide.BUY
                qty = delta
            else:
                side = OrderSide.SELL
                qty = -delta

            if qty <= 0:
                continue

            order = PendingOrder(
                order_id=f"O-{sig.signal_id}",
                signal_id=sig.signal_id,
                instrument_id=sig.instrument_id,
                asset_type=sig.asset_type,
                side=side,
                target_quantity=qty,
                limit_price=None,
                ttl=1,
                created_at=datetime.combine(sig.signal_date, datetime.min.time()),
                execution_date=exec_date,
            )

            exec_, trade, cash, positions = execute_order(
                order,
                exec_bar,
                config,
                cash,
                positions,
            )
            executions.append(exec_)
            if trade is not None:
                trades.append(trade)
                gross_turnover += trade.quantity * trade.raw_price

        # 收盘按 raw close 估值并保存每日账本。
        marked_close: dict[str, Position] = {}
        for instrument_id, position in positions.items():
            mark = exec_bars.get(instrument_id)
            market_value = (
                (position.available_quantity + position.frozen_quantity) * mark.close
                if mark is not None
                else position.market_value
            )
            marked_close[instrument_id] = position.model_copy(
                update={"market_value": market_value}
            )
        positions = marked_close
        positions_value = sum(position.market_value for position in positions.values())
        snapshots.append(
            PortfolioSnapshot(
                trade_date=exec_date,
                cash=cash,
                frozen_cash=0.0,
                positions=tuple(positions.values()),
                total_market_value=positions_value,
                net_value=cash + positions_value,
                data_version=data_version,
            )
        )

    metrics = _performance_metrics(snapshots, config.initial_cash)
    metrics["total_trades"] = float(len(trades))
    metrics["turnover"] = (
        gross_turnover / sum(item.net_value for item in snapshots) if snapshots else 0.0
    )
    if benchmark_bars:
        benchmark = sorted(
            (
                item
                for item in benchmark_bars
                if item.adjustment is BarAdjustment.QFQ
                and item.data_version == data_version
            ),
            key=lambda item: item.trade_date,
        )
        metrics["benchmark_return"] = (
            benchmark[-1].close / benchmark[0].close - 1.0
            if len(benchmark) >= 2
            else 0.0
        )

    return BacktestResult(
        trades=tuple(trades),
        executions=tuple(executions),
        snapshots=tuple(snapshots),
        metrics=metrics,
    )


def _performance_metrics(
    snapshots: list[PortfolioSnapshot], initial_cash: float
) -> dict[str, float]:
    if not snapshots:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "annual_volatility": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
            "calmar_ratio": 0.0,
        }
    nav = [initial_cash, *(item.net_value for item in snapshots)]
    returns = [nav[index] / nav[index - 1] - 1.0 for index in range(1, len(nav))]
    total_return = nav[-1] / initial_cash - 1.0
    periods = max(len(returns), 1)
    annual_return = (1.0 + total_return) ** (252.0 / periods) - 1.0
    mean = sum(returns) / len(returns) if returns else 0.0
    variance = (
        sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        if len(returns) > 1
        else 0.0
    )
    annual_volatility = math.sqrt(variance) * math.sqrt(252.0)
    sharpe = mean / math.sqrt(variance) * math.sqrt(252.0) if variance > 0 else 0.0
    peak = nav[0]
    max_drawdown = 0.0
    for value in nav:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1.0)
    calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": annual_volatility,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_drawdown,
        "calmar_ratio": calmar,
    }

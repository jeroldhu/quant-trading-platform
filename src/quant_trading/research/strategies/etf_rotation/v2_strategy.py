"""ETF 行业轮动 V2 策略引擎。

完整实现十三层规则：
  1. 基础过滤  2. 横截面因子标准化  3. 综合评分  4. 标的选择
  5. 仓位分配  6. 市场风险开关    7. 组合回撤控制  8. 冷静期恢复
  9. 换仓控制  10. 调仓频率      11. 交易成本      12. 最小持仓周期
  13. 恢复机制

所有信号在 T 日收盘后产生，T+1 日开盘执行。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

import numpy as np
import pandas as pd

from .v2_config import V2StrategyConfig, DEFAULT_CONFIG


# ==============================================================================
# 数据容器
# ==============================================================================


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class DrawdownZone(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"     # >= 8%
    SEVERE = "SEVERE"       # >= 12%
    CRASH = "CRASH"         # >= 15%


class MarketRegime(StrEnum):
    BULL = "BULL"           # Close > MA60 & MA20 > MA60
    WEAK = "WEAK"           # Close < MA60 & MA20 >= MA60
    BEAR = "BEAR"           # Close < MA60 & MA20 < MA60
    TRANSITION = "TRANSITION"  # Close > MA60 & MA20 < MA60


@dataclass
class ETFBar:
    """单只 ETF 单日行情。"""
    instrument_id: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float


@dataclass
class HoldingPosition:
    """当前持仓状态。"""
    instrument_id: str
    quantity: int
    avg_cost: float
    entry_date: date
    entry_score: float = 0.0
    holding_days: int = 0


@dataclass
class Order:
    """待执行订单。"""
    instrument_id: str
    side: OrderSide
    quantity: int
    target_weight: float
    score: float
    reason: str


@dataclass
class Trade:
    """已成交订单。"""
    trade_date: date
    instrument_id: str
    side: OrderSide
    quantity: int
    price: float
    fee: float
    gross_amount: float


@dataclass
class DailySnapshot:
    """每日组合状态快照。"""
    trade_date: date
    cash: float
    positions: dict[str, HoldingPosition]
    total_market_value: float
    net_value: float
    portfolio_peak: float
    portfolio_drawdown: float
    drawdown_zone: DrawdownZone
    market_regime: MarketRegime
    target_total_weight: float
    final_total_weight: float
    cooldown_remaining: int
    signals: list[Order]
    trades: list[Trade]


@dataclass
class BacktestResult:
    """回测完整结果。"""
    snapshots: list[DailySnapshot] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    annual_returns: dict[int, float] = field(default_factory=dict)
    monthly_returns: dict[str, float] = field(default_factory=dict)


# ==============================================================================
# 因子计算（pandas 向量化）
# ==============================================================================


def _compute_indicators_df(df: pd.DataFrame, cfg: V2StrategyConfig) -> pd.DataFrame:
    """用 pandas 向量化方法为单只 ETF 计算全部技术指标。

    输入 DataFrame 需包含列: open, high, low, close, volume, amount
    返回原 DataFrame 并附加所有指标列。
    """
    result = df.copy()
    close = result["close"]
    high = result["high"]
    low = result["low"]
    amount = result["amount"]

    # 均线
    result["ma20"] = close.rolling(cfg.ma_short, min_periods=cfg.ma_short).mean()
    result["ma60"] = close.rolling(cfg.ma_long, min_periods=cfg.ma_long).mean()

    # 收益率
    result["ret_20d"] = close / close.shift(cfg.ma_short) - 1.0
    result["ret_60d"] = close / close.shift(cfg.ma_long) - 1.0

    # 波动率
    daily_ret = close.pct_change()
    result["volatility_20d"] = daily_ret.rolling(cfg.ma_short, min_periods=cfg.ma_short).std(ddof=0) * math.sqrt(252)

    # 最大回撤 (60日)
    rolling_peak_60 = close.rolling(cfg.ma_long, min_periods=cfg.ma_long).max()
    result["mdd_60d"] = close / rolling_peak_60 - 1.0
    # abs_mdd: 过去 60 日内最小的 drawdown（绝对值最大的回撤）
    result["abs_mdd_60d"] = result["mdd_60d"].rolling(cfg.ma_long, min_periods=cfg.ma_long).apply(
        lambda x: abs(x.min()), raw=True
    )

    # MA60 斜率
    slope_lb = cfg.ma_slope_lookback
    result["ma60_slope"] = result["ma60"].rolling(slope_lb, min_periods=slope_lb).apply(
        lambda y: float(np.polyfit(np.arange(slope_lb, dtype=np.float64), y.values, 1)[0])
        if len(y) == slope_lb and y.notna().all() else np.nan,
        raw=False,
    )
    result["ma60_slope_pct"] = result["ma60_slope"] / result["ma60"].rolling(slope_lb, min_periods=slope_lb).mean()

    # ATR20
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    result["atr20"] = tr.rolling(cfg.ma_short, min_periods=cfg.ma_short).mean()

    # 过度偏离
    result["distance_atr"] = (close - result["ma20"]) / result["atr20"]
    result["overextension_penalty"] = (result["distance_atr"] - cfg.overextension_atr_mult).clip(lower=0)

    # 趋势质量
    result["tq_raw"] = result["ma20"] / result["ma60"] - 1.0

    # 20 日平均成交额
    result["amount_ma20"] = amount.rolling(cfg.ma_short, min_periods=cfg.ma_short).mean()

    return result


# ==============================================================================
# 百分位排名
# ==============================================================================


def percentile_rank(values: Sequence[float | None]) -> list[float]:
    """横截面百分位排名，NaN 映射为 0.5（中性）。

    使用 average 方法：同值共享相同排名。
    """
    n = len(values)
    if n == 0:
        return []

    # 分离有效值和索引
    valid_pairs = [(i, v) for i, v in enumerate(values) if v is not None and not math.isnan(v)]
    if not valid_pairs:
        return [0.5] * n

    valid_indices, valid_values = zip(*valid_pairs)
    valid_values = list(valid_values)

    # 排序
    sorted_pairs = sorted(enumerate(valid_values), key=lambda x: x[1])
    ranks = [0.0] * len(valid_values)
    i = 0
    while i < len(sorted_pairs):
        j = i
        while j < len(sorted_pairs) and sorted_pairs[j][1] == sorted_pairs[i][1]:
            j += 1
        avg_rank = (i + j - 1) / 2.0 / (len(sorted_pairs) - 1) if len(sorted_pairs) > 1 else 0.5
        for k in range(i, j):
            ranks[sorted_pairs[k][0]] = avg_rank
        i = j

    # 组装结果
    result = [0.5] * n
    for orig_idx, rank_val in zip(valid_indices, ranks):
        result[orig_idx] = rank_val

    return result


# ==============================================================================
# 策略引擎
# ==============================================================================


class V2EtfRotationEngine:
    """V2 ETF 行业轮动策略引擎。

    每日推进，维护完整状态机（持仓/回撤/冷静期/恢复）。
    """

    def __init__(
        self,
        instruments: Sequence[str],
        benchmark_id: str,
        config: V2StrategyConfig | None = None,
    ) -> None:
        self.instruments = tuple(instruments)
        self.benchmark_id = benchmark_id
        self.cfg = config or DEFAULT_CONFIG

        # 状态
        self.cash = self.cfg.initial_cash
        self.positions: dict[str, HoldingPosition] = {}
        self.portfolio_peak = self.cfg.initial_cash
        self.cooldown_remaining = 0
        self.current_drawdown_zone = DrawdownZone.NORMAL
        self.days_in_recovery: dict[DrawdownZone, int] = {
            DrawdownZone.WARNING: 0,
            DrawdownZone.SEVERE: 0,
        }

        # 历史记录
        self.snapshots: list[DailySnapshot] = []
        self.all_trades: list[Trade] = []

        # 数据缓存: instrument_id -> DataFrame (date-indexed)
        self._etf_data: dict[str, pd.DataFrame] = {}
        self._benchmark_data: pd.DataFrame | None = None
        self._all_dates: list[date] = []

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def load_data(
        self,
        bars: Sequence[ETFBar],
        benchmark_bars: Sequence[ETFBar],
    ) -> None:
        """加载完整历史数据并预计算所有指标。"""
        # ETF 数据
        etf_frames: dict[str, list[dict[str, object]]] = {}
        for bar in bars:
            etf_frames.setdefault(bar.instrument_id, []).append({
                "trade_date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
            })

        for inst_id, rows in etf_frames.items():
            df = pd.DataFrame(rows)
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.sort_values("trade_date").set_index("trade_date")
            # 预计算全部技术指标
            self._etf_data[inst_id] = _compute_indicators_df(df, self.cfg)

        # 基准数据
        bench_rows = [
            {
                "trade_date": b.trade_date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "amount": b.amount,
            }
            for b in benchmark_bars
        ]
        if bench_rows:
            bm_df = pd.DataFrame(bench_rows)
            bm_df["trade_date"] = pd.to_datetime(bm_df["trade_date"])
            bm_df = bm_df.sort_values("trade_date").set_index("trade_date")
            self._benchmark_data = _compute_indicators_df(bm_df, self.cfg)

        # 全部交易日
        all_dates_set: set[date] = set()
        for df in self._etf_data.values():
            all_dates_set.update(d.date() for d in df.index)
        if self._benchmark_data is not None:
            all_dates_set.update(d.date() for d in self._benchmark_data.index)
        self._all_dates = sorted(all_dates_set)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> BacktestResult:
        """逐日运行回测。"""
        if not self._all_dates:
            raise ValueError("未加载数据，请先调用 load_data()")

        # 找到第一个所有指标都可计算的日期
        min_bars = self.cfg.ma_long + self.cfg.ma_slope_lookback + 5
        start_idx = 0
        for i, d in enumerate(self._all_dates):
            if i >= min_bars:
                start_idx = i
                break

        for i in range(start_idx, len(self._all_dates)):
            trade_date = self._all_dates[i]
            self._process_day(trade_date)

        return self._build_result()

    def _process_day(self, trade_date: date) -> None:
        """处理单个交易日。"""
        # 更新持仓天数
        for pos in self.positions.values():
            pos.holding_days += 1

        # 1. 冷静期检查
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            if self.cooldown_remaining == 0:
                # 冷静期结束，重置组合峰值到当前水平，避免立即再次触发
                self.portfolio_peak = self._current_equity(trade_date)
                self.current_drawdown_zone = DrawdownZone.NORMAL
            self._record_snapshot(trade_date, [], [], MarketRegime.BEAR, 0.0, 0.0)
            return

        # 2. 计算基准指标 → 市场状态
        regime, bench_indicators = self._market_regime(trade_date)
        if regime is None:
            self._record_snapshot(trade_date, [], [], MarketRegime.BEAR, 0.0, 0.0)
            return

        # 3. 先计算当前组合状态（每日执行的风控）
        current_equity = self._current_equity(trade_date)
        self.portfolio_peak = max(self.portfolio_peak, current_equity)
        portfolio_dd = (current_equity / self.portfolio_peak - 1.0) if self.portfolio_peak > 0 else 0.0
        abs_dd = abs(portfolio_dd)

        # ---- 每日风险检查 ----
        # 检查是否需要强制卖出（持仓不再满足过滤条件）
        force_exit_orders = self._check_risk_exits(trade_date)

        # 组合回撤崩溃检查
        if abs_dd >= self.cfg.dd_crash_level:
            self.cooldown_remaining = self.cfg.cooldown_days
            self.current_drawdown_zone = DrawdownZone.CRASH
            all_exits = self._exit_all(trade_date, f"组合回撤 {abs_dd:.2%} >= {self.cfg.dd_crash_level:.0%}，清仓冷静期")
            self._record_snapshot(trade_date, all_exits, [], regime, self._market_target_weight(regime), 0.0)
            self._execute_orders(all_exits, trade_date)
            self.days_in_recovery = {DrawdownZone.WARNING: 0, DrawdownZone.SEVERE: 0}
            return

        # 更新回撤区域和恢复状态
        new_zone = self._compute_drawdown_zone(abs_dd)
        if new_zone != self.current_drawdown_zone:
            self.current_drawdown_zone = new_zone
            self.days_in_recovery = {DrawdownZone.WARNING: 0, DrawdownZone.SEVERE: 0}

        drawdown_limit = self._drawdown_position_limit()
        market_target = self._market_target_weight(regime)

        # ---- 调仓频率控制 ----
        is_rebalance_day = self._is_rebalance_day(trade_date)

        if not is_rebalance_day:
            # 非调仓日：仅执行风险卖出，不产生新买入信号
            if force_exit_orders:
                self._record_snapshot(trade_date, force_exit_orders, [], regime, market_target, 0.0)
                self._execute_orders(force_exit_orders, trade_date)
            else:
                self._record_snapshot(trade_date, [], [], regime, market_target, min(market_target, drawdown_limit))
            return

        # ---- 调仓日：完整信号生成 ----
        # 4. 计算所有 ETF 指标 → 过滤 → 评分
        candidates = self._screen_candidates(trade_date)

        if candidates.empty:
            orders = self._exit_all(trade_date, "无 ETF 通过基础过滤")
            self._record_snapshot(trade_date, orders, [], regime, market_target, 0.0)
            self._execute_orders(orders, trade_date)
            return

        # 7. 最终仓位 = min(市场目标, 回撤上限)
        final_total = min(market_target, drawdown_limit)

        # 8. 选 Top N + 等权分配
        selected = self._select_top(candidates)

        # 9. 分配目标仓位
        target_weights = self._allocate_weights(selected, final_total)

        # 10. 应用换仓阈值和最小持仓周期
        orders = self._generate_orders(target_weights, candidates, trade_date)

        # 合并风险卖出和调仓订单
        all_orders = force_exit_orders + orders

        # 11. 执行并记录
        self._record_snapshot(trade_date, all_orders, [], regime, market_target, final_total)
        self._execute_orders(all_orders, trade_date)

    # ------------------------------------------------------------------
    # 基准与市场状态
    # ------------------------------------------------------------------

    def _market_regime(self, trade_date: date) -> tuple[MarketRegime | None, dict]:
        if self._benchmark_data is None:
            return MarketRegime.BULL, {}

        df = self._benchmark_data
        ts = pd.Timestamp(trade_date)
        if ts not in df.index:
            return None, {}

        row = df.loc[ts]
        close = float(row["close"])
        ma20 = float(row["ma20"])
        ma60 = float(row["ma60"])

        if pd.isna(ma20) or pd.isna(ma60):
            return None, {}

        if close > ma60 and ma20 > ma60:
            regime = MarketRegime.BULL
        elif close < ma60 and ma20 >= ma60:
            regime = MarketRegime.WEAK
        elif close < ma60 and ma20 < ma60:
            regime = MarketRegime.BEAR
        else:
            regime = MarketRegime.TRANSITION

        return regime, {"ma20": ma20, "ma60": ma60, "close": close}

    def _market_target_weight(self, regime: MarketRegime) -> float:
        if regime == MarketRegime.BULL:
            return self.cfg.market_bull_total
        elif regime == MarketRegime.WEAK:
            return self.cfg.market_weak_total
        elif regime == MarketRegime.BEAR:
            return self.cfg.market_bear_total
        else:
            # TRANSITION: MA20 < MA60 but Close > MA60
            return self.cfg.market_weak_total

    # ------------------------------------------------------------------
    # ETF 筛选与评分
    # ------------------------------------------------------------------

    def _screen_candidates(self, trade_date: date) -> pd.DataFrame:
        """第一层过滤 + 第二层因子计算 + 第三层综合评分。

        使用预计算的指标 DataFrame，不再每次重新计算。
        """
        ts = pd.Timestamp(trade_date)
        rows = []
        for inst_id in self.instruments:
            df = self._etf_data.get(inst_id)
            if df is None or df.empty:
                continue
            if ts not in df.index:
                continue

            row = df.loc[ts]

            # 提取指标值
            ret20 = float(row["ret_20d"])
            ret60 = float(row["ret_60d"])
            vol = float(row["volatility_20d"])
            abs_mdd = float(row["abs_mdd_60d"])
            ma20 = float(row["ma20"])
            ma60 = float(row["ma60"])
            close = float(row["close"])
            slope = float(row["ma60_slope_pct"])
            ext_penalty = float(row["overextension_penalty"])
            amount_ma20_val = float(row["amount_ma20"])
            tq = float(row["tq_raw"])

            # 检查 NaN
            if any(math.isnan(v) for v in [ret20, ret60, vol, abs_mdd, ma20, ma60, slope, ext_penalty, amount_ma20_val, tq]):
                continue

            # ---- 第一层：基础过滤 ----
            listing_days = len(df.loc[:ts])
            if listing_days < self.cfg.min_listing_days:
                continue
            if amount_ma20_val <= self.cfg.min_amount_ma20:
                continue
            if close <= ma60:
                continue
            if ma20 <= ma60:
                continue
            if slope <= 0:
                continue

            rows.append({
                "instrument_id": inst_id,
                "ret_20d": ret20,
                "ret_60d": ret60,
                "volatility_20d": vol,
                "abs_mdd_60d": abs_mdd,
                "tq_raw": tq,
                "overextension_penalty": ext_penalty,
                "amount_ma20": amount_ma20_val,
                "listing_days": listing_days,
            })

        if not rows:
            return pd.DataFrame()

        result_df = pd.DataFrame(rows)

        # ---- 第二层：横截面百分位排名 ----
        result_df["M20"] = percentile_rank(result_df["ret_20d"].tolist())
        result_df["M60"] = percentile_rank(result_df["ret_60d"].tolist())
        result_df["TQ"] = percentile_rank(result_df["tq_raw"].tolist())
        result_df["VOL"] = percentile_rank(result_df["volatility_20d"].tolist())
        result_df["MDD"] = percentile_rank(result_df["abs_mdd_60d"].tolist())
        result_df["EXT"] = percentile_rank(result_df["overextension_penalty"].tolist())

        # ---- 第三层：综合评分 ----
        result_df["score"] = (
            self.cfg.weight_m20 * result_df["M20"]
            + self.cfg.weight_m60 * result_df["M60"]
            + self.cfg.weight_tq * result_df["TQ"]
            - self.cfg.weight_vol * result_df["VOL"]
            - self.cfg.weight_mdd * result_df["MDD"]
            - self.cfg.weight_ext * result_df["EXT"]
        )

        return result_df.sort_values("score", ascending=False)

    # ------------------------------------------------------------------
    # 标的选择
    # ------------------------------------------------------------------

    def _select_top(self, candidates: pd.DataFrame) -> pd.DataFrame:
        return candidates.head(self.cfg.top_n)

    # ------------------------------------------------------------------
    # 仓位分配
    # ------------------------------------------------------------------

    def _allocate_weights(
        self, selected: pd.DataFrame, total_weight: float
    ) -> dict[str, dict]:
        """等权分配，受单只 40% 上限约束。"""
        n = len(selected)
        if n == 0:
            return {}

        each = total_weight / n
        each = min(each, self.cfg.max_single_weight)

        result = {}
        for _, row in selected.iterrows():
            inst_id = str(row["instrument_id"])
            result[inst_id] = {
                "weight": each,
                "score": float(row["score"]),
                "ret_20d": float(row["ret_20d"]),
                "ret_60d": float(row["ret_60d"]),
            }
        return result

    # ------------------------------------------------------------------
    # 订单生成（含换仓阈值 & 最小持仓周期）
    # ------------------------------------------------------------------

    def _generate_orders(
        self,
        target_weights: dict[str, dict],
        candidates: pd.DataFrame,
        trade_date: date,
    ) -> list[Order]:
        """生成买卖订单，应用第 9-11 层规则。"""
        orders = []
        current_holdings = set(self.positions.keys())
        target_holdings = set(target_weights.keys())

        # 将 candidates 转为 dict 便于查分
        score_map = dict(zip(candidates["instrument_id"], candidates["score"]))

        # ---- 卖出检查 ----
        for inst_id in current_holdings - target_holdings:
            orders.append(Order(
                instrument_id=inst_id,
                side=OrderSide.SELL,
                quantity=self.positions[inst_id].quantity,
                target_weight=0.0,
                score=0.0,
                reason="不再入选 Top2",
            ))

        # ---- 买入 / 换仓检查 ----
        for inst_id in target_holdings:
            tw = target_weights[inst_id]
            target_score = float(tw["score"])

            if inst_id in self.positions:
                pos = self.positions[inst_id]

                # 基础过滤失败 → 立即卖出
                force_exit = self._position_failed_filter(inst_id, trade_date)

                if force_exit:
                    orders.append(Order(
                        instrument_id=inst_id,
                        side=OrderSide.SELL,
                        quantity=pos.quantity,
                        target_weight=0.0,
                        score=target_score,
                        reason="持仓不满足基础过滤，强制卖出",
                    ))
                    continue

                # 最小持仓周期内 + 评分下降不大 → 持有
                if pos.holding_days < self.cfg.min_holding_days:
                    if target_score >= pos.entry_score - self.cfg.turnover_threshold:
                        # 保持不变，无需交易
                        continue

                # 换仓阈值：新得分需要比当前高 0.05
                if target_score <= pos.entry_score + self.cfg.turnover_threshold:
                    continue

            # 计算目标数量
            equity = self._current_equity(trade_date)
            target_value = equity * target_weights[inst_id]["weight"]
            df = self._etf_data.get(inst_id)
            if df is None:
                continue

            # T+1 开盘价（用当日收盘近似，实际回测在 execute 时用次日开盘价）
            idx_arr = df.index.get_indexer([pd.Timestamp(trade_date)], method="ffill")
            if idx_arr[0] < 0:
                continue
            loc = df.index[idx_arr[0]]
            pos_idx = df.index.tolist().index(loc)
            if pos_idx + 1 >= len(df):
                continue

            next_open = float(df["open"].iloc[pos_idx + 1])
            if next_open <= 0:
                continue

            target_qty = int(target_value / next_open / self.cfg.lot_size) * self.cfg.lot_size
            if target_qty <= 0:
                continue

            # 如果已持有，计算差额
            if inst_id in self.positions:
                current_qty = self.positions[inst_id].quantity
                delta = target_qty - current_qty
                if delta > 0:
                    orders.append(Order(
                        instrument_id=inst_id,
                        side=OrderSide.BUY,
                        quantity=delta,
                        target_weight=tw["weight"],
                        score=target_score,
                        reason=f"调仓加仓 (评分 {target_score:.4f})",
                    ))
                elif delta < 0:
                    orders.append(Order(
                        instrument_id=inst_id,
                        side=OrderSide.SELL,
                        quantity=abs(delta),
                        target_weight=tw["weight"],
                        score=target_score,
                        reason=f"调仓减仓 (评分 {target_score:.4f})",
                    ))
            else:
                orders.append(Order(
                    instrument_id=inst_id,
                    side=OrderSide.BUY,
                    quantity=target_qty,
                    target_weight=tw["weight"],
                    score=target_score,
                    reason=f"新入选 (评分 {target_score:.4f})",
                ))

        return orders

    def _position_failed_filter(self, inst_id: str, trade_date: date) -> bool:
        """检查持仓是否仍满足基础过滤条件。"""
        df = self._etf_data.get(inst_id)
        if df is None:
            return True

        ts = pd.Timestamp(trade_date)
        if ts not in df.index:
            return True

        row = df.loc[ts]
        close = float(row["close"])
        ma20 = float(row["ma20"])
        ma60 = float(row["ma60"])
        slope = float(row["ma60_slope_pct"])
        amount_ma20_val = float(row["amount_ma20"])

        if any(math.isnan(v) for v in [close, ma20, ma60]):
            return True

        return (
            close <= ma60
            or ma20 <= ma60
            or (slope <= 0)
            or (amount_ma20_val <= self.cfg.min_amount_ma20)
        )

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _execute_orders(self, orders: list[Order], trade_date: date) -> None:
        """T+1 开盘执行订单。"""
        # 按 trade_date 的下一天找执行日
        exec_date = self._next_trade_date(trade_date)
        if exec_date is None:
            return

        for order in orders:
            if order.side == OrderSide.SELL:
                self._execute_sell(order, exec_date)
            else:
                self._execute_buy(order, exec_date)

    def _execute_buy(self, order: Order, exec_date: date) -> None:
        df = self._etf_data.get(order.instrument_id)
        if df is None:
            return

        date_indices = df.index.tolist()
        try:
            idx = date_indices.index(pd.Timestamp(exec_date))
        except ValueError:
            return

        price = float(df["open"].iloc[idx])
        volume = int(df["volume"].iloc[idx])

        # 流动性限制
        max_qty = int(volume * self.cfg.max_volume_participation / self.cfg.lot_size) * self.cfg.lot_size
        qty = min(order.quantity, max_qty)
        if qty <= 0:
            return

        gross = qty * price
        fee = gross * self.cfg.buy_commission + self.cfg.slippage * gross
        fee = max(fee, self.cfg.min_commission)

        if gross + fee > self.cash:
            # 缩量买入
            affordable_qty = int((self.cash - fee) / price / self.cfg.lot_size) * self.cfg.lot_size
            if affordable_qty <= 0:
                return
            qty = affordable_qty
            gross = qty * price
            fee = gross * self.cfg.buy_commission + self.cfg.slippage * gross
            fee = max(fee, self.cfg.min_commission)

        self.cash -= gross + fee

        if order.instrument_id in self.positions:
            pos = self.positions[order.instrument_id]
            total_qty = pos.quantity + qty
            pos.quantity = total_qty
            pos.avg_cost = (pos.avg_cost * (total_qty - qty) + gross) / total_qty
            pos.holding_days = 0  # 加仓重置持仓天数
            pos.entry_score = order.score
        else:
            self.positions[order.instrument_id] = HoldingPosition(
                instrument_id=order.instrument_id,
                quantity=qty,
                avg_cost=price,
                entry_date=exec_date,
                entry_score=order.score,
                holding_days=0,
            )

        self.all_trades.append(Trade(
            trade_date=exec_date,
            instrument_id=order.instrument_id,
            side=OrderSide.BUY,
            quantity=qty,
            price=price,
            fee=fee,
            gross_amount=gross,
        ))

    def _execute_sell(self, order: Order, exec_date: date) -> None:
        pos = self.positions.get(order.instrument_id)
        if pos is None:
            return

        df = self._etf_data.get(order.instrument_id)
        if df is None:
            return

        date_indices = df.index.tolist()
        try:
            idx = date_indices.index(pd.Timestamp(exec_date))
        except ValueError:
            return

        price = float(df["open"].iloc[idx])
        qty = min(order.quantity, pos.quantity)
        if qty <= 0:
            return

        gross = qty * price
        fee = gross * self.cfg.sell_commission + self.cfg.slippage * gross
        fee = max(fee, self.cfg.min_commission)

        self.cash += gross - fee
        pos.quantity -= qty

        if pos.quantity <= 0:
            del self.positions[order.instrument_id]

        self.all_trades.append(Trade(
            trade_date=exec_date,
            instrument_id=order.instrument_id,
            side=OrderSide.SELL,
            quantity=qty,
            price=price,
            fee=fee,
            gross_amount=gross,
        ))

    # ------------------------------------------------------------------
    # 回撤控制
    # ------------------------------------------------------------------

    def _compute_drawdown_zone(self, abs_dd: float) -> DrawdownZone:
        if abs_dd >= self.cfg.dd_severe_level:
            return DrawdownZone.SEVERE
        elif abs_dd >= self.cfg.dd_warning_level:
            return DrawdownZone.WARNING
        return DrawdownZone.NORMAL

    def _drawdown_position_limit(self) -> float:
        """根据当前回撤区域和恢复状态返回仓位上限。"""
        if self.current_drawdown_zone == DrawdownZone.SEVERE:
            return self.cfg.dd_severe_cap
        elif self.current_drawdown_zone == DrawdownZone.WARNING:
            return self.cfg.dd_warning_cap
        return 1.0

    # ------------------------------------------------------------------
    # 退出全部持仓
    # ------------------------------------------------------------------

    def _exit_all(self, trade_date: date, reason: str) -> list[Order]:
        return [
            Order(
                instrument_id=inst_id,
                side=OrderSide.SELL,
                quantity=pos.quantity,
                target_weight=0.0,
                score=0.0,
                reason=reason,
            )
            for inst_id, pos in self.positions.items()
            if pos.quantity > 0
        ]

    # ------------------------------------------------------------------
    # 记录与辅助
    # ------------------------------------------------------------------

    def _current_equity(self, trade_date: date) -> float:
        """当前日期收盘估值。"""
        positions_value = 0.0
        for inst_id, pos in self.positions.items():
            df = self._etf_data.get(inst_id)
            if df is None:
                continue
            idx_arr = df.index.get_indexer([pd.Timestamp(trade_date)], method="ffill")
            if idx_arr[0] < 0:
                continue
            loc = df.index[idx_arr[0]]
            price = float(df.loc[loc, "close"])
            positions_value += pos.quantity * price
        return self.cash + positions_value

    def _record_snapshot(
        self,
        trade_date: date,
        orders: list[Order],
        trades: list[Trade],
        regime: MarketRegime,
        market_target: float,
        final_total: float,
    ) -> None:
        equity = self._current_equity(trade_date)
        self.portfolio_peak = max(self.portfolio_peak, equity)
        dd = (equity / self.portfolio_peak - 1.0) if self.portfolio_peak > 0 else 0.0
        zone = self._compute_drawdown_zone(abs(dd))

        self.snapshots.append(DailySnapshot(
            trade_date=trade_date,
            cash=self.cash,
            positions=dict(self.positions),
            total_market_value=equity - self.cash,
            net_value=equity,
            portfolio_peak=self.portfolio_peak,
            portfolio_drawdown=dd,
            drawdown_zone=zone,
            market_regime=regime,
            target_total_weight=market_target,
            final_total_weight=final_total,
            cooldown_remaining=self.cooldown_remaining,
            signals=orders,
            trades=trades,
        ))

    # ------------------------------------------------------------------
    # 调仓频率 & 风险检查
    # ------------------------------------------------------------------

    def _is_rebalance_day(self, trade_date: date) -> bool:
        """判断是否为调仓日。

        模式 A (daily): 每天调仓
        模式 B (weekly): 每周最后一个交易日调仓
        """
        if self.cfg.rebalance_frequency == "daily":
            return True

        if self.cfg.rebalance_frequency == "weekly":
            # 找到本周最后一个交易日
            weekday = trade_date.weekday()  # 0=Mon, 4=Fri
            # 检查今天之后本周是否还有交易日
            week_end = trade_date
            for d in self._all_dates:
                if d > trade_date and d.isocalendar()[1] == trade_date.isocalendar()[1]:
                    week_end = d
                elif d.isocalendar()[1] != trade_date.isocalendar()[1]:
                    break
            return trade_date >= week_end

        return True

    def _check_risk_exits(self, trade_date: date) -> list[Order]:
        """每日检查持仓是否触发风险卖出条件。"""
        orders = []
        for inst_id, pos in list(self.positions.items()):
            if self._position_failed_filter(inst_id, trade_date):
                orders.append(Order(
                    instrument_id=inst_id,
                    side=OrderSide.SELL,
                    quantity=pos.quantity,
                    target_weight=0.0,
                    score=0.0,
                    reason=f"不满足基础过滤，强制卖出",
                ))
        return orders

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _next_trade_date(self, current: date) -> date | None:
        """找 current 之后的下一个交易日。"""
        for d in self._all_dates:
            if d > current:
                return d
        return None

    # ------------------------------------------------------------------
    # 绩效
    # ------------------------------------------------------------------

    def _build_result(self) -> BacktestResult:
        snaps = self.snapshots
        if not snaps:
            return BacktestResult()

        nav = [self.cfg.initial_cash] + [s.net_value for s in snaps]
        returns = [nav[i] / nav[i - 1] - 1.0 for i in range(1, len(nav))]

        total_return = nav[-1] / self.cfg.initial_cash - 1.0
        n_periods = len(returns)
        annual_return = (1 + total_return) ** (252 / max(n_periods, 1)) - 1.0

        mean_ret = sum(returns) / max(len(returns), 1)
        var = sum((r - mean_ret) ** 2 for r in returns) / max(len(returns) - 1, 1)
        annual_vol = math.sqrt(var) * math.sqrt(252)
        sharpe = mean_ret / math.sqrt(var) * math.sqrt(252) if var > 0 else 0.0

        # Sortino
        downside_returns = [r for r in returns if r < 0]
        downside_var = sum(r ** 2 for r in downside_returns) / max(len(downside_returns) - 1, 1) if len(downside_returns) > 1 else var
        sortino = mean_ret / math.sqrt(downside_var) * math.sqrt(252) if downside_var > 0 else 0.0

        peak_val = nav[0]
        max_dd = 0.0
        for v in nav:
            peak_val = max(peak_val, v)
            max_dd = min(max_dd, v / peak_val - 1.0)
        calmar = annual_return / abs(max_dd) if max_dd < 0 else 0.0

        buy_trades = [t for t in self.all_trades if t.side == OrderSide.BUY]
        sell_trades = [t for t in self.all_trades if t.side == OrderSide.SELL]
        win_count = 0
        for bt, st in zip(
            sorted(buy_trades, key=lambda t: (t.instrument_id, t.trade_date)),
            sorted(sell_trades, key=lambda t: (t.instrument_id, t.trade_date)),
        ):
            pnl = (st.price * st.quantity - st.fee) - (bt.price * bt.quantity + bt.fee)
            if pnl > 0:
                win_count += 1
        total_round_trips = min(len(buy_trades), len(sell_trades))
        win_rate = win_count / total_round_trips if total_round_trips > 0 else 0.0

        # 换手率
        total_gross = sum(t.gross_amount for t in self.all_trades)
        avg_nav = sum(s.net_value for s in snaps) / max(len(snaps), 1)
        turnover = total_gross / avg_nav if avg_nav > 0 else 0.0

        # 年/月收益
        dates = [s.trade_date for s in snaps]
        nav_values = [s.net_value for s in snaps]
        nav_series = pd.Series(nav_values, index=pd.DatetimeIndex(dates))
        if len(nav_series) >= 2:
            daily_ret_series = nav_series.pct_change().dropna()
            annual_returns: dict[int, float] = {}
            for yr, group in daily_ret_series.groupby(daily_ret_series.index.year):
                annual_returns[int(yr)] = float((1 + group).prod() - 1)
            monthly_returns: dict[str, float] = {}
            for (yr, mo), group in daily_ret_series.groupby(
                [(daily_ret_series.index.year), (daily_ret_series.index.month)]
            ):
                monthly_returns[f"{yr}-{mo:02d}"] = float((1 + group).prod() - 1)
        else:
            annual_returns = {}
            monthly_returns = {}

        return BacktestResult(
            snapshots=self.snapshots,
            trades=self.all_trades,
            metrics={
                "total_return": total_return,
                "annual_return": annual_return,
                "annual_volatility": annual_vol,
                "sharpe_ratio": sharpe,
                "sortino_ratio": sortino,
                "max_drawdown": max_dd,
                "calmar_ratio": calmar,
                "win_rate": win_rate,
                "total_trades": float(len(self.all_trades)),
                "turnover": turnover,
            },
            annual_returns=annual_returns,
            monthly_returns=monthly_returns,
        )

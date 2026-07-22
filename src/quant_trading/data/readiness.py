"""数据就绪门禁的计算、持久化与强制阻断。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from quant_trading.data.storage import ParquetDuckDBStore


class ReadinessGate(StrEnum):
    DAILY_MARKET_READY = "DAILY_MARKET_READY"
    FEATURE_READY = "FEATURE_READY"
    ROTATION_READY = "ROTATION_READY"
    CROSS_SECTION_READY = "CROSS_SECTION_READY"
    STOCK_BACKTEST_READY = "STOCK_BACKTEST_READY"


class ReadinessState(StrEnum):
    PENDING = "PENDING"
    PROVISIONAL = "PROVISIONAL"
    READY = "READY"
    BLOCKED = "BLOCKED"
    DEGRADED = "DEGRADED"


class ReadinessError(RuntimeError):
    """正式运行所需数据未达到门禁。"""


@dataclass(frozen=True, slots=True)
class ReadinessStatus:
    gate: ReadinessGate
    trade_date: date
    data_version: str
    state: ReadinessState
    coverage: float = 0.0
    published_count: int = 0
    expected_count: int = 0
    blocking_issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state in {ReadinessState.READY, ReadinessState.DEGRADED}


def require_readiness(status: ReadinessStatus) -> None:
    if not status.ready:
        detail = "; ".join(status.blocking_issues) or status.state.value
        raise ReadinessError(
            f"{status.gate.value} 未通过 ({status.data_version}): {detail}"
        )


class ReadinessRegistry:
    """DuckDB 持久化门禁注册表；不再依赖进程内状态。"""

    def __init__(self, store: ParquetDuckDBStore) -> None:
        self._store = store

    def set(self, status: ReadinessStatus) -> None:
        if self._store.read_only:
            raise PermissionError("只读研究端禁止修改门禁")
        with self._store.connect() as connection:
            self._store._bootstrap_catalog(connection)
            connection.execute(
                """
                INSERT OR REPLACE INTO data_readiness
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    status.gate.value,
                    status.trade_date,
                    status.data_version,
                    status.state.value,
                    status.coverage,
                    status.published_count,
                    status.expected_count,
                    json.dumps(status.blocking_issues, ensure_ascii=False),
                    json.dumps(status.warnings, ensure_ascii=False),
                    datetime.now(UTC),
                ],
            )

    def get(
        self,
        gate: ReadinessGate,
        trade_date: date,
        data_version: str | None = None,
    ) -> ReadinessStatus:
        if not self._store.catalog_path.exists():
            return self._pending(gate, trade_date, data_version or "")
        where_version = "AND data_version = ?" if data_version else ""
        params: list[object] = [gate.value, trade_date]
        if data_version:
            params.append(data_version)
        with self._store.connect() as connection:
            row = connection.execute(
                f"""
                SELECT data_version, state, coverage, published_count, expected_count,
                       blocking_issues, warnings
                FROM data_readiness
                WHERE gate = ? AND trade_date = ? {where_version}
                ORDER BY evaluated_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        if row is None:
            return self._pending(gate, trade_date, data_version or "")
        return ReadinessStatus(
            gate=gate,
            trade_date=trade_date,
            data_version=str(row[0]),
            state=ReadinessState(str(row[1])),
            coverage=float(row[2]),
            published_count=int(row[3]),
            expected_count=int(row[4]),
            blocking_issues=tuple(json.loads(str(row[5]))),
            warnings=tuple(json.loads(str(row[6]))),
        )

    @staticmethod
    def _pending(
        gate: ReadinessGate, trade_date: date, data_version: str
    ) -> ReadinessStatus:
        return ReadinessStatus(
            gate=gate,
            trade_date=trade_date,
            data_version=data_version,
            state=ReadinessState.PENDING,
            blocking_issues=("门禁尚未评估",),
        )

    def require(
        self,
        gate: ReadinessGate,
        trade_date: date,
        data_version: str | None = None,
    ) -> None:
        require_readiness(self.get(gate, trade_date, data_version))


def _state(blocking: list[str], warnings: list[str]) -> ReadinessState:
    if blocking:
        return ReadinessState.BLOCKED
    if warnings:
        return ReadinessState.DEGRADED
    return ReadinessState.READY


def build_daily_market_ready(
    trade_date: date,
    data_version: str,
    published_instruments: int,
    expected_instruments: int,
    independent_sources: int,
    *,
    eastmoney_non_empty: bool,
    warnings: tuple[str, ...] = (),
    required_source_failures: tuple[str, ...] = (),
    min_coverage: float = 0.99,
) -> ReadinessStatus:
    blocking: list[str] = []
    warning_list = list(warnings)
    coverage = (
        published_instruments / expected_instruments if expected_instruments else 0.0
    )
    if expected_instruments <= 0:
        blocking.append("point-in-time 全市场 ETF 数量为 0")
    if coverage < min_coverage:
        blocking.append(f"覆盖率 {coverage:.2%} < {min_coverage:.2%}")
    if not eastmoney_non_empty:
        blocking.append("东方财富当日行情为空")
    if independent_sources < 2:
        blocking.append(f"独立来源数 {independent_sources} < 2")
    if required_source_failures:
        blocking.append(
            "必需来源不可用: " + ", ".join(sorted(required_source_failures))
        )
    return ReadinessStatus(
        gate=ReadinessGate.DAILY_MARKET_READY,
        trade_date=trade_date,
        data_version=data_version,
        state=_state(blocking, warning_list),
        coverage=min(coverage, 1.0),
        published_count=published_instruments,
        expected_count=expected_instruments,
        blocking_issues=tuple(blocking),
        warnings=tuple(warning_list),
    )


def build_feature_ready(
    trade_date: date,
    data_version: str,
    complete_instruments: int,
    expected_instruments: int,
    *,
    missing_raw_days: int,
    missing_qfq_days: int,
    history_bars: int,
    min_history_bars: int,
) -> ReadinessStatus:
    blocking: list[str] = []
    if missing_raw_days:
        blocking.append(f"raw 缺失 {missing_raw_days} 个交易日")
    if missing_qfq_days:
        blocking.append(f"qfq 缺失 {missing_qfq_days} 个交易日")
    if history_bars < min_history_bars:
        blocking.append(f"历史长度 {history_bars} < {min_history_bars}")
    coverage = (
        complete_instruments / expected_instruments if expected_instruments else 0.0
    )
    return ReadinessStatus(
        ReadinessGate.FEATURE_READY,
        trade_date,
        data_version,
        _state(blocking, []),
        coverage,
        complete_instruments,
        expected_instruments,
        tuple(blocking),
    )


def build_rotation_ready(
    trade_date: date,
    data_version: str,
    candidate_coverage: int,
    required_count: int,
    benchmark_bars: int,
    calendar_conflicts: int,
    *,
    raw_qfq_complete: bool = False,
    twenty_day_coverage: float = 0.0,
    max_return_difference: float = 1.0,
) -> ReadinessStatus:
    blocking: list[str] = []
    if candidate_coverage < required_count:
        blocking.append(f"候选覆盖 {candidate_coverage}/{required_count}")
    if not raw_qfq_complete:
        blocking.append("候选 ETF raw/qfq 未同时完整")
    if benchmark_bars < 252:
        blocking.append(f"沪深 300 历史 {benchmark_bars} < 252 日")
    if calendar_conflicts:
        blocking.append(f"交易日历冲突 {calendar_conflicts} 日")
    if twenty_day_coverage < 0.98:
        blocking.append(f"候选 20 日双源覆盖率 {twenty_day_coverage:.2%} < 98%")
    if max_return_difference > 0.003:
        blocking.append(f"双源收益最大差异 {max_return_difference:.4f} > 0.003")
    coverage = candidate_coverage / required_count if required_count else 0.0
    return ReadinessStatus(
        ReadinessGate.ROTATION_READY,
        trade_date,
        data_version,
        _state(blocking, []),
        min(coverage, 1.0),
        candidate_coverage,
        required_count,
        tuple(blocking),
    )


def build_cross_section_ready(
    trade_date: date,
    data_version: str,
    complete_instruments: int,
    eligible_instruments: int,
    *,
    unevaluable_new_listings: int = 0,
) -> ReadinessStatus:
    blocking = []
    if eligible_instruments <= 0:
        blocking.append("point-in-time 全市场 ETF 范围为空")
    if complete_instruments < eligible_instruments:
        blocking.append(
            "全市场 61 日 raw/qfq 双源历史 "
            f"{complete_instruments}/{eligible_instruments}"
        )
    warnings = (
        (f"{unevaluable_new_listings} 只上市不足 61 日，标记不可评估",)
        if unevaluable_new_listings
        else ()
    )
    coverage = (
        complete_instruments / eligible_instruments if eligible_instruments else 0.0
    )
    return ReadinessStatus(
        ReadinessGate.CROSS_SECTION_READY,
        trade_date,
        data_version,
        _state(blocking, list(warnings)),
        coverage,
        complete_instruments,
        eligible_instruments,
        tuple(blocking),
        warnings,
    )


def build_stock_backtest_ready(
    trade_date: date,
    data_version: str,
    complete_instruments: int,
    expected_instruments: int,
    *,
    limit_api_ready: bool,
    suspension_api_ready: bool,
    st_api_ready: bool,
) -> ReadinessStatus:
    """股票回测门禁：双口径行情与三类交易约束缺一不可。"""
    blocking: list[str] = []
    if expected_instruments <= 0:
        blocking.append("主题股票范围为空")
    if complete_instruments < expected_instruments:
        blocking.append(
            f"股票 raw/qfq 双源历史 {complete_instruments}/{expected_instruments}"
        )
    if not limit_api_ready:
        blocking.append("stk_limit 涨跌停价格不可用")
    if not suspension_api_ready:
        blocking.append("suspend_d 停复牌信息不可用")
    if not st_api_ready:
        blocking.append("stock_st 历史 ST 列表不可用")
    coverage = (
        complete_instruments / expected_instruments if expected_instruments else 0.0
    )
    return ReadinessStatus(
        ReadinessGate.STOCK_BACKTEST_READY,
        trade_date,
        data_version,
        _state(blocking, []),
        min(coverage, 1.0),
        complete_instruments,
        expected_instruments,
        tuple(blocking),
    )

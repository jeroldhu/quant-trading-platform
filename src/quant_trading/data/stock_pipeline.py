"""主题股票数据管道。

Tushare 提供 raw、复权因子以及涨跌停/停牌/ST 官方约束；公开行情源只负责
价格交叉验证。任何权限失败都会保留 Raw 并阻断 STOCK_BACKTEST_READY。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from uuid import uuid4

from quant_trading.config import AppSettings, PipelineConfig, load_pipeline_config
from quant_trading.data.providers import (
    BarProvider,
    EastmoneyProvider,
    FetchResult,
    FetchState,
    ProviderFailure,
    SourceBar,
    TdxProvider,
    TencentProvider,
    ThsProvider,
    TushareStockProvider,
    normalize_instrument,
)
from quant_trading.data.readiness import (
    ReadinessRegistry,
    build_stock_backtest_ready,
    require_readiness,
)
from quant_trading.data.storage import ParquetDuckDBStore
from quant_trading.data.validation import fuse_gold_bars
from quant_trading.models import BarAdjustment


@dataclass(frozen=True, slots=True)
class StockPipelineRun:
    run_id: str
    data_version: str
    published_count: int
    state: str


class StockPipelineOrchestrator:
    """显式股票范围的 Raw→Gold 编排器，不从当前板块反推历史成员。"""

    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        config: PipelineConfig | None = None,
        store: ParquetDuckDBStore | None = None,
        tushare: TushareStockProvider | None = None,
        price_providers: Sequence[BarProvider] | None = None,
    ) -> None:
        self.settings = settings or AppSettings()
        self.config = config or load_pipeline_config()
        self.store = store or ParquetDuckDBStore(
            self.settings.data_root, min_free_gb=self.settings.min_free_gb
        )
        token = self.settings.tushare_token
        if tushare is None and not token:
            raise ValueError("股票正式管道需要 QUANT_TUSHARE_TOKEN")
        self.tushare = tushare or TushareStockProvider(
            token or "",
            timeout_seconds=self.settings.request_timeout_seconds,
            requests_per_second=self.settings.requests_per_second,
        )
        self.price_providers = tuple(
            price_providers
            if price_providers is not None
            else (
                EastmoneyProvider(
                    timeout_seconds=self.settings.request_timeout_seconds,
                    requests_per_second=self.settings.requests_per_second,
                ),
                TencentProvider(
                    timeout_seconds=self.settings.request_timeout_seconds,
                    requests_per_second=self.settings.requests_per_second,
                ),
                ThsProvider(
                    timeout_seconds=self.settings.request_timeout_seconds,
                    requests_per_second=self.settings.requests_per_second,
                ),
                TdxProvider(),
            )
        )

    @staticmethod
    def _run_id(end: date) -> str:
        return f"stock-{end:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"

    @staticmethod
    def _version(end: date, hashes: Sequence[str]) -> str:
        digest = hashlib.sha256(
            json.dumps(sorted(hashes), separators=(",", ":")).encode()
        ).hexdigest()[:12]
        return f"stock-{end:%Y%m%d}-{digest}"

    def run(
        self, instruments: Sequence[str], start: date, end: date
    ) -> StockPipelineRun:
        if start > end:
            raise ValueError("start 不能晚于 end")
        resolved = tuple(sorted({normalize_instrument(item) for item in instruments}))
        if not resolved:
            raise ValueError("股票范围不能为空")
        self.store.bootstrap()
        self.store.ensure_free_space()
        run_id = self._run_id(end)
        registered_version: str | None = None
        with self.store.writer_lock():
            self.store.start_run(run_id, "stock-backfill", end, "PENDING")
            try:
                bars: list[SourceBar] = []
                hashes: list[str] = []
                constraint_states: dict[str, list[bool]] = {
                    "tushare_stk_limit": [],
                    "tushare_suspend_d": [],
                    "tushare_stock_st": [],
                }
                for instrument in resolved:
                    raw_results, bundle = self.tushare.fetch_stock_bundle(
                        instrument, start, end
                    )
                    for raw_result in (*raw_results, bundle):
                        self.store.save_raw(raw_result, run_id)
                        hashes.append(raw_result.raw_hash)
                        if raw_result.source_id in constraint_states:
                            constraint_states[raw_result.source_id].append(
                                raw_result.state is FetchState.SUCCESS
                            )
                    if bundle.state is not FetchState.SUCCESS:
                        raise RuntimeError(
                            f"{instrument} Tushare 股票约束不可用: "
                            f"{bundle.error_detail or bundle.state.value}"
                        )
                    bars.extend(bundle.bars)

                for adjustment in (BarAdjustment.RAW, BarAdjustment.QFQ):
                    for provider in self.price_providers:
                        if adjustment not in provider.supports_adjustments:
                            continue
                        try:
                            results = provider.fetch_daily_bars(
                                resolved, start, end, adjustment
                            )
                        except ProviderFailure:
                            continue
                        for original in results:
                            stock_result: FetchResult = replace(
                                original, dataset="stock_daily_bar"
                            )
                            self.store.save_raw(stock_result, run_id)
                            hashes.append(stock_result.raw_hash)
                            if stock_result.state is FetchState.SUCCESS:
                                bars.extend(stock_result.bars)

                data_version = self._version(end, hashes)
                self.store.update_run_version(run_id, data_version)
                existing_version = data_version in self.store.available_versions()
                if not existing_version:
                    self.store.begin_data_version(
                        data_version,
                        parent_data_version=self.store.latest_ready_data_version(),
                        command="stock-backfill",
                    )
                    registered_version = data_version
                self.store.upsert_bronze(bars, data_version, dataset="stock_daily_bar")
                fusion = fuse_gold_bars(
                    bars,
                    data_version=data_version,
                    min_independent_sources=self.config.quality.min_independent_sources,
                )
                self.store.append_silver(
                    bars,
                    data_version,
                    fusion.quality_by_key,
                    dataset="stock_daily_bar",
                )
                published = self.store.append_stock_bars(fusion.gold_bars)
                current: dict[str, set[str]] = {}
                for bar in fusion.gold_bars:
                    if bar.trade_date == end:
                        current.setdefault(bar.instrument_id, set()).add(
                            bar.adjustment.value
                        )
                complete = sum(values == {"raw", "qfq"} for values in current.values())
                status = build_stock_backtest_ready(
                    end,
                    data_version,
                    complete,
                    len(resolved),
                    limit_api_ready=bool(constraint_states["tushare_stk_limit"])
                    and all(constraint_states["tushare_stk_limit"]),
                    suspension_api_ready=bool(constraint_states["tushare_suspend_d"])
                    and all(constraint_states["tushare_suspend_d"]),
                    st_api_ready=bool(constraint_states["tushare_stock_st"])
                    and all(constraint_states["tushare_stock_st"]),
                )
                ReadinessRegistry(self.store).set(status)
                require_readiness(status)
                if not existing_version:
                    self.store.finish_data_version(data_version, "READY")
                self.store.finish_run(
                    run_id,
                    "SUCCESS",
                    f"gold={published}; coverage={status.coverage:.2%}",
                )
            except Exception as exc:
                if registered_version is not None:
                    self.store.finish_data_version(registered_version, "FAILED")
                self.store.finish_run(run_id, "FAILED", str(exc))
                raise
        return StockPipelineRun(run_id, data_version, published, status.state.value)

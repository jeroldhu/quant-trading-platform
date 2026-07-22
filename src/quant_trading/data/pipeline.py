"""Raw → Bronze → Silver → Gold 数据管道编排。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from quant_trading.config import (
    AppSettings,
    PipelineConfig,
    load_pipeline_config,
    load_universe_config,
)
from quant_trading.data.calendar import (
    build_official_trade_calendar,
    calendar_config_hash,
    is_official_trade_day,
    previous_official_trade_day,
)
from quant_trading.data.features import build_etf_features
from quant_trading.data.providers import (
    BarProvider,
    EastmoneyProvider,
    FetchState,
    InstrumentRecord,
    MasterProvider,
    MockBarProvider,
    ProviderFailure,
    SourceBar,
    TdxProvider,
    TencentProvider,
    ThsProvider,
    normalize_instrument,
)
from quant_trading.data.readiness import (
    ReadinessGate,
    ReadinessRegistry,
    ReadinessStatus,
    build_cross_section_ready,
    build_daily_market_ready,
    build_feature_ready,
    build_rotation_ready,
    build_stock_backtest_ready,
    require_readiness,
)
from quant_trading.data.storage import ParquetDuckDBStore
from quant_trading.data.validation import FusionResult, fuse_gold_bars
from quant_trading.models import BarAdjustment, MarketBar, TradingStatus

DATA_DIRECTORIES = ("raw", "bronze", "silver", "gold", "catalog", "staging")


@dataclass(frozen=True, slots=True)
class PipelineRun:
    run_id: str
    command: str
    trade_date: date
    data_version: str
    published_count: int
    state: str


def bootstrap_data_root(data_root: Path) -> tuple[Path, ...]:
    return ParquetDuckDBStore(data_root).bootstrap()


class PipelineOrchestrator:
    """生产管道组合根；Provider 可注入，以支持确定性的离线验收。"""

    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        config: PipelineConfig | None = None,
        store: ParquetDuckDBStore | None = None,
        providers: Sequence[BarProvider] | None = None,
        master_provider: MasterProvider | None = None,
        master_providers: Sequence[MasterProvider] | None = None,
        offline_fixture: bool = False,
    ) -> None:
        self.settings = settings or AppSettings()
        self.config = config or load_pipeline_config()
        self.store = store or ParquetDuckDBStore(
            self.settings.data_root, min_free_gb=self.settings.min_free_gb
        )
        self.registry = ReadinessRegistry(self.store)
        self.offline_fixture = offline_fixture
        self.providers: tuple[BarProvider, ...]
        if providers is not None:
            self.providers = tuple(providers)
        elif offline_fixture:
            self.providers = (
                MockBarProvider("fixture_a", "fixture-a.local"),
                MockBarProvider("fixture_b", "fixture-b.local"),
            )
        else:
            configured_providers: dict[str, BarProvider] = {
                "eastmoney": EastmoneyProvider(
                    timeout_seconds=self.settings.request_timeout_seconds,
                    requests_per_second=self.settings.requests_per_second,
                ),
                "tencent": TencentProvider(
                    timeout_seconds=self.settings.request_timeout_seconds,
                    requests_per_second=self.settings.requests_per_second,
                ),
                "ths": ThsProvider(
                    timeout_seconds=self.settings.request_timeout_seconds,
                    requests_per_second=self.settings.requests_per_second,
                ),
                "tdx": TdxProvider(),
            }
            self.providers = tuple(
                provider
                for name, provider in configured_providers.items()
                if (requirement := self.config.sources.get(name)) is not None
                and requirement.enabled
            )
            if not self.providers:
                raise ValueError("没有启用任何行情数据源")
        if master_providers is not None:
            self.master_providers = tuple(master_providers)
        elif master_provider is not None:
            self.master_providers = (master_provider,)
        elif offline_fixture:
            self.master_providers = ()
        else:
            candidates: dict[str, MasterProvider] = {
                "eastmoney": EastmoneyProvider(
                    timeout_seconds=self.settings.request_timeout_seconds,
                    requests_per_second=self.settings.requests_per_second,
                ),
                "tdx": TdxProvider(),
            }
            self.master_providers = tuple(
                provider
                for name, provider in candidates.items()
                if (requirement := self.config.sources.get(name)) is not None
                and requirement.enabled
            )
        if not offline_fixture and any(
            isinstance(provider, MockBarProvider) for provider in self.providers
        ):
            raise ValueError("正式模式禁止注册 MockBarProvider")

    @staticmethod
    def _run_id(command: str, trade_date: date) -> str:
        return f"{command}-{trade_date.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"

    def _resolve_instruments(
        self,
        as_of: date,
        override: Sequence[str] | None,
    ) -> tuple[str, ...]:
        if override:
            return tuple(sorted({normalize_instrument(item) for item in override}))
        configured = tuple(
            normalize_instrument(item)
            for item in self.config.collection.configured_instruments
        )
        if configured:
            return tuple(sorted(set(configured)))
        universe = self.store.get_universe(as_of)
        if universe:
            return universe
        if self.offline_fixture:
            raise RuntimeError(
                "离线 fixture 必须通过 --instruments 指定范围，不能伪造全市场清单"
            )
        self.refresh_master(as_of)
        universe = self.store.get_universe(as_of)
        if not universe:
            raise RuntimeError("etf_master_scd 为空，无法解析 point-in-time 全市场范围")
        return universe

    def refresh_master(self, as_of: date) -> PipelineRun:
        if len(self.master_providers) < 2:
            raise RuntimeError("正式 ETF master 至少需要两个独立数据源")
        self.store.bootstrap()
        run_id = self._run_id("refresh-master", as_of)
        with self.store.writer_lock():
            self.store.start_run(run_id, "refresh-master", as_of, "PENDING")
            try:
                hashes: list[str] = []
                records: list[InstrumentRecord] = []
                failures: list[str] = []
                for provider in self.master_providers:
                    for result in provider.fetch_master(as_of):
                        self.store.save_raw(result, run_id)
                        hashes.append(result.raw_hash)
                        if result.state is FetchState.SUCCESS:
                            records.extend(result.instruments)
                        else:
                            failures.append(
                                f"{result.source_id}:{result.state.value}"
                                + (
                                    f"({result.error_detail})"
                                    if result.error_detail
                                    else ""
                                )
                            )
                if failures:
                    raise RuntimeError("ETF master 来源未通过: " + "; ".join(failures))
                if not records:
                    raise RuntimeError(
                        "ETF master 数据源返回空，拒绝更新 point-in-time 范围"
                    )
                version = self._data_version("master", as_of, hashes)
                self.store.update_run_version(run_id, version)
                count = self.store.append_master(records, version)
                self.store.refresh_views()
                self.store.finish_run(run_id, "SUCCESS", f"master_records={count}")
            except Exception as exc:
                self.store.finish_run(run_id, "FAILED", str(exc))
                raise
        return PipelineRun(run_id, "refresh-master", as_of, version, count, "SUCCESS")

    @staticmethod
    def _data_version(command: str, end: date, raw_hashes: Sequence[str]) -> str:
        digest = hashlib.sha256(
            json.dumps(sorted(raw_hashes), separators=(",", ":")).encode()
        ).hexdigest()[:12]
        return f"{command}-{end.strftime('%Y%m%d')}-{digest}"

    def _fetch(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        run_id: str,
        adjustments: Sequence[BarAdjustment] = (
            BarAdjustment.RAW,
            BarAdjustment.QFQ,
        ),
    ) -> tuple[list[SourceBar], list[str], list[str], set[str]]:
        bars: list[SourceBar] = []
        hashes: list[str] = []
        warnings: list[str] = []
        failed_providers: set[str] = set()
        for adjustment in adjustments:
            for provider in self.providers:
                if adjustment not in provider.supports_adjustments:
                    continue
                try:
                    results = provider.fetch_daily_bars(
                        instruments, start, end, adjustment
                    )
                except ProviderFailure as exc:
                    warnings.append(str(exc))
                    failed_providers.add(provider.source_id)
                    continue
                for result in results:
                    self.store.save_raw(result, run_id)
                    hashes.append(result.raw_hash)
                    if result.state is FetchState.SUCCESS:
                        bars.extend(result.bars)
                    elif result.state is not FetchState.NO_DATA:
                        warnings.append(
                            f"{result.source_id}: {result.state.value}"
                            + (
                                f" ({result.error_detail})"
                                if result.error_detail
                                else ""
                            )
                        )
                        failed_providers.add(provider.source_id)
                    else:
                        failed_providers.add(provider.source_id)
        return bars, hashes, warnings, failed_providers

    def _execute(
        self,
        command: str,
        start: date,
        end: date,
        instruments: Sequence[str] | None,
        *,
        enforce_full_market_gate: bool,
    ) -> PipelineRun:
        if start > end:
            raise ValueError("start 不能晚于 end")
        if command == "daily" and not is_official_trade_day(end):
            raise RuntimeError(f"{end} 不是交易所公告确认的交易日，拒绝采集")
        self.store.bootstrap()
        self.store.ensure_free_space()
        if command == "backfill" and instruments is None:
            resolved = self.store.get_universe_for_range(start, end)
            readiness_instruments = self._resolve_instruments(end, None)
        else:
            resolved = self._resolve_instruments(end, instruments)
            readiness_instruments = resolved
        if not resolved:
            raise RuntimeError("本次任务证券范围为空")
        run_id = self._run_id(command, end)
        registered_version: str | None = None
        with self.store.writer_lock():
            self.store.start_run(run_id, command, end, "PENDING")
            try:
                bars, hashes, warnings, failed_providers = self._fetch(
                    resolved, start, end, run_id
                )
                benchmark_source_bars: list[SourceBar] = []
                if "index_daily" in self.config.collection.datasets:
                    benchmark_source_bars, benchmark_hashes, benchmark_warnings, _ = (
                        self._fetch(
                            self.config.collection.benchmarks,
                            start,
                            end,
                            run_id,
                            adjustments=(BarAdjustment.RAW,),
                        )
                    )
                    hashes.extend(benchmark_hashes)
                    warnings.extend(benchmark_warnings)
                if not hashes:
                    raise RuntimeError("所有数据源均失败或未产生可审计原始响应")
                if "trade_calendar" in self.config.collection.datasets:
                    hashes.append(calendar_config_hash())
                data_version = self._data_version(command, end, hashes)
                self.store.update_run_version(run_id, data_version)
                existing_version = data_version in self.store.available_versions()
                if not existing_version:
                    self.store.begin_data_version(
                        data_version,
                        parent_data_version=self.store.latest_ready_data_version(),
                        command=command,
                    )
                    registered_version = data_version
                self.store.upsert_bronze(bars, data_version)
                fusion = fuse_gold_bars(
                    bars,
                    data_version=data_version,
                    min_independent_sources=self.config.quality.min_independent_sources,
                )
                self.store.record_quality_issues(
                    run_id,
                    [
                        {
                            "instrument_id": issue.instrument_id,
                            "trade_date": issue.trade_date,
                            "adjustment": issue.adjustment,
                            "issue_code": issue.issue_code,
                            "severity": issue.severity.value,
                            "detail": issue.detail,
                            "sources": issue.sources,
                        }
                        for issue in fusion.issues
                    ],
                )
                self.store.append_silver(bars, data_version, fusion.quality_by_key)
                if benchmark_source_bars:
                    benchmark_fusion = fuse_gold_bars(
                        benchmark_source_bars,
                        data_version=data_version,
                        min_independent_sources=(
                            self.config.quality.min_independent_sources
                        ),
                        price_abs_tolerance=0.01,
                    )
                    self.store.append_index_bars(benchmark_fusion.gold_bars)
                    self.store.record_quality_issues(
                        run_id,
                        [
                            {
                                "instrument_id": issue.instrument_id,
                                "trade_date": issue.trade_date,
                                "adjustment": issue.adjustment,
                                "issue_code": issue.issue_code,
                                "severity": issue.severity.value,
                                "detail": issue.detail,
                                "sources": issue.sources,
                            }
                            for issue in benchmark_fusion.issues
                        ],
                    )
                if "trade_calendar" in self.config.collection.datasets:
                    self.store.append_trade_calendar(
                        build_official_trade_calendar(
                            start,
                            end + timedelta(days=14),
                            data_version=data_version,
                        )
                    )
                readiness = self._daily_readiness(
                    end,
                    data_version,
                    readiness_instruments,
                    fusion,
                    warnings,
                    failed_providers,
                )
                self.registry.set(readiness)
                if enforce_full_market_gate:
                    require_readiness(readiness)
                published = self.store.append_bars(fusion.gold_bars)
                feature_history_start = date.fromisoformat(self.config.history.start)
                qfq_history = self.store.get_bars_during_publish(
                    resolved,
                    feature_history_start,
                    end,
                    adjustment=BarAdjustment.QFQ,
                    data_version=data_version,
                )
                benchmark_history: Sequence[MarketBar] = ()
                if self.config.collection.benchmarks:
                    benchmark_history = self.store.get_index_bars_during_publish(
                        self.config.collection.benchmarks[0],
                        feature_history_start,
                        end,
                        data_version=data_version,
                    )
                feature_rows = build_etf_features(
                    qfq_history,
                    benchmark_history,
                    data_version=data_version,
                )
                features_published = self.store.append_features(
                    [
                        row
                        for row in feature_rows
                        if start
                        <= date.fromisoformat(str(row["trade_date"])[:10])
                        <= end
                    ]
                )
                self.evaluate_research_readiness(end, data_version)
                detail = (
                    f"gold={published}; coverage={readiness.coverage:.2%}; "
                    f"features={features_published}; issues={len(fusion.issues)}"
                )
                if not existing_version:
                    self.store.finish_data_version(data_version, "READY")
                self.store.finish_run(run_id, "SUCCESS", detail)
            except Exception as exc:
                if registered_version is not None:
                    self.store.finish_data_version(registered_version, "FAILED")
                self.store.finish_run(run_id, "FAILED", str(exc))
                raise
        return PipelineRun(
            run_id,
            command,
            end,
            data_version,
            published,
            readiness.state.value,
        )

    def _daily_readiness(
        self,
        trade_date: date,
        data_version: str,
        instruments: Sequence[str],
        fusion: FusionResult,
        warnings: Sequence[str],
        failed_providers: set[str],
    ) -> ReadinessStatus:
        by_instrument: dict[str, set[str]] = {}
        source_counts: list[int] = []
        for bar in fusion.gold_bars:
            if bar.trade_date != trade_date:
                continue
            by_instrument.setdefault(bar.instrument_id, set()).add(bar.adjustment.value)
            source_counts.append(bar.source_count)
        complete = sum(
            adjustments == {BarAdjustment.RAW.value, BarAdjustment.QFQ.value}
            for adjustments in by_instrument.values()
        )
        eastmoney_non_empty = any(
            any(source.startswith("eastmoney") for source in item.source_map)
            for item in fusion.gold_bars
            if item.trade_date == trade_date
        )
        if self.offline_fixture:
            eastmoney_non_empty = True
        required_failures = tuple(
            sorted(
                source
                for source in failed_providers
                if (requirement := self.config.sources.get(source)) is not None
                and requirement.enabled
                and requirement.required
            )
        )
        return build_daily_market_ready(
            trade_date,
            data_version,
            complete,
            len(instruments),
            min(source_counts, default=0),
            eastmoney_non_empty=eastmoney_non_empty,
            warnings=tuple(warnings),
            required_source_failures=required_failures,
            min_coverage=self.config.quality.min_publish_coverage,
        )

    def daily(
        self, trade_date: date, instruments: Sequence[str] | None = None
    ) -> PipelineRun:
        """采集目标交易日全市场 raw/qfq，门禁失败时禁止发布 Gold。"""

        enforce = instruments is None and self.config.collection.scope == "full_market"
        return self._execute(
            "daily",
            previous_official_trade_day(
                trade_date, self.config.incremental.overlap_trade_days
            ),
            trade_date,
            instruments,
            enforce_full_market_gate=enforce,
        )

    def evaluate_research_readiness(
        self, trade_date: date, data_version: str, *, persist: bool = True
    ) -> tuple[ReadinessStatus, ...]:
        """从持久化 Gold/Silver 计算策略门禁，候选数始终来自配置。"""

        universe_config = load_universe_config()
        rotation_entry = universe_config.universes.get("etf_rotation")
        candidates = tuple(rotation_entry.instruments) if rotation_entry else ()
        gold = self.store.resolve_versioned_dataset(
            "gold",
            "etf_daily_bar",
            data_version,
            business_key=("instrument_id", "trade_date", "adjustment"),
            include_pending=True,
        )
        index = self.store.resolve_versioned_dataset(
            "gold",
            "index_daily",
            data_version,
            business_key=("instrument_id", "trade_date", "adjustment"),
            include_pending=True,
        )
        calendar = self.store.resolve_versioned_dataset(
            "gold",
            "trade_calendar",
            data_version,
            business_key=("calendar_date",),
            include_pending=True,
        )
        silver = self.store.resolve_versioned_dataset(
            "silver",
            "etf_daily_bar",
            data_version,
            business_key=(
                "instrument_id",
                "trade_date",
                "adjustment",
                "source_id",
            ),
            include_pending=True,
        )
        for frame in (gold, index, calendar, silver):
            if not frame.empty and "trade_date" in frame:
                frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        if not calendar.empty:
            calendar["calendar_date"] = pd.to_datetime(
                calendar["calendar_date"]
            ).dt.date

        version_gold = (
            gold.loc[gold["data_version"] == data_version].copy()
            if not gold.empty
            else pd.DataFrame(
                columns=["instrument_id", "trade_date", "adjustment", "data_version"]
            )
        )
        current = version_gold.loc[
            (version_gold["trade_date"] == trade_date)
            & version_gold["instrument_id"].isin(candidates)
        ]
        adjustment_counts = current.groupby("instrument_id")["adjustment"].nunique()
        candidate_complete = int((adjustment_counts >= 2).sum())

        version_index = (
            index.loc[index["data_version"] == data_version].copy()
            if not index.empty
            else pd.DataFrame(columns=["trade_date", "data_version"])
        )
        benchmark_bars = (
            int(
                version_index.loc[version_index["trade_date"] <= trade_date][
                    "trade_date"
                ].nunique()
            )
            if not version_index.empty
            else 0
        )
        version_calendar = (
            calendar.loc[calendar["data_version"] == data_version].copy()
            if not calendar.empty
            else pd.DataFrame(
                columns=[
                    "calendar_date",
                    "data_version",
                    "has_conflict",
                    "is_trade_day",
                ]
            )
        )
        calendar_conflicts = int(
            version_calendar.get("has_conflict", pd.Series(dtype=bool))
            .fillna(False)
            .sum()
        )

        history = version_gold.loc[
            version_gold["instrument_id"].isin(candidates)
            & (version_gold["trade_date"] <= trade_date)
        ]
        required_points = len(candidates) * 2 * 20
        observed_points = 0
        for _, group in history.groupby(["instrument_id", "adjustment"]):
            observed_points += min(int(group["trade_date"].nunique()), 20)
        twenty_day_coverage = (
            observed_points / required_points if required_points else 0.0
        )
        max_return_difference = self._max_source_return_difference(
            silver, candidates, trade_date, data_version
        )
        rotation = build_rotation_ready(
            trade_date,
            data_version,
            candidate_complete,
            len(candidates),
            benchmark_bars,
            calendar_conflicts,
            raw_qfq_complete=candidate_complete == len(candidates),
            twenty_day_coverage=twenty_day_coverage,
            max_return_difference=max_return_difference,
        )

        raw_counts = (
            history.loc[history.get("adjustment") == "raw"]
            .groupby("instrument_id")["trade_date"]
            .nunique()
        )
        qfq_counts = (
            history.loc[history.get("adjustment") == "qfq"]
            .groupby("instrument_id")["trade_date"]
            .nunique()
        )
        feature_complete = sum(
            int(raw_counts.get(item, 0)) >= 61 and int(qfq_counts.get(item, 0)) >= 61
            for item in candidates
        )
        feature = build_feature_ready(
            trade_date,
            data_version,
            feature_complete,
            len(candidates),
            missing_raw_days=sum(
                max(61 - int(raw_counts.get(item, 0)), 0) for item in candidates
            ),
            missing_qfq_days=sum(
                max(61 - int(qfq_counts.get(item, 0)), 0) for item in candidates
            ),
            history_bars=min(
                (int(qfq_counts.get(item, 0)) for item in candidates), default=0
            ),
            min_history_bars=61,
        )

        universe_members = self.store.get_universe_members(trade_date)
        confirmed_days = (
            sorted(
                day
                for day in version_calendar.loc[
                    version_calendar["is_trade_day"].astype(bool)
                ]["calendar_date"]
                if day <= trade_date
            )
            if not version_calendar.empty
            else []
        )
        eligible: list[str] = []
        new_listings = 0
        for instrument, listing_date in universe_members.items():
            listed_trade_days = (
                sum(day >= listing_date for day in confirmed_days)
                if listing_date is not None
                else 61
            )
            if listed_trade_days >= 61:
                eligible.append(instrument)
            else:
                new_listings += 1
        full_history = version_gold.loc[
            version_gold["instrument_id"].isin(eligible)
            & (version_gold["trade_date"] <= trade_date)
        ]
        full_raw_counts = (
            full_history.loc[full_history.get("adjustment") == "raw"]
            .groupby("instrument_id")["trade_date"]
            .nunique()
        )
        full_qfq_counts = (
            full_history.loc[full_history.get("adjustment") == "qfq"]
            .groupby("instrument_id")["trade_date"]
            .nunique()
        )
        cross_complete = sum(
            int(full_raw_counts.get(item, 0)) >= 61
            and int(full_qfq_counts.get(item, 0)) >= 61
            for item in eligible
        )
        cross = build_cross_section_ready(
            trade_date,
            data_version,
            cross_complete,
            len(eligible),
            unevaluable_new_listings=new_listings,
        )
        stock = self.store.resolve_versioned_dataset(
            "gold",
            "stock_daily_bar",
            data_version,
            business_key=("instrument_id", "trade_date", "adjustment"),
            include_pending=True,
        )
        try:
            theme_members = self.store.get_theme_members(
                trade_date,
                data_version=data_version,
                board_types=("concept", "industry"),
            )
            theme_instruments = tuple(
                sorted({item for values in theme_members.values() for item in values})
            )
        except RuntimeError:
            theme_instruments = ()
        if not stock.empty:
            stock["trade_date"] = pd.to_datetime(stock["trade_date"]).dt.date
        stock_history = (
            stock.loc[
                stock["instrument_id"].isin(theme_instruments)
                & (stock["trade_date"] <= trade_date)
            ]
            if not stock.empty
            else pd.DataFrame()
        )
        stock_complete = 0
        tushare_constraints_ready = False
        if not stock_history.empty:
            counts = stock_history.groupby(["instrument_id", "adjustment"])[
                "trade_date"
            ].nunique()
            stock_complete = sum(
                int(counts.get((instrument, "raw"), 0)) >= 61
                and int(counts.get((instrument, "qfq"), 0)) >= 61
                for instrument in theme_instruments
            )
            source_maps = stock_history.get("source_map", pd.Series(dtype=str))
            tushare_constraints_ready = any(
                "tushare_stock_" in str(value) for value in source_maps
            )
        stock_ready = build_stock_backtest_ready(
            trade_date,
            data_version,
            stock_complete,
            len(theme_instruments),
            limit_api_ready=tushare_constraints_ready,
            suspension_api_ready=tushare_constraints_ready,
            st_api_ready=tushare_constraints_ready,
        )
        if persist:
            for status in (rotation, feature, cross, stock_ready):
                self.registry.set(status)
        return rotation, feature, cross, stock_ready

    @staticmethod
    def _max_source_return_difference(
        silver: pd.DataFrame,
        candidates: Sequence[str],
        trade_date: date,
        data_version: str,
    ) -> float:
        if silver.empty:
            return 1.0
        selected = silver.loc[
            (silver["data_version"] == data_version)
            & silver["instrument_id"].isin(candidates)
            & (silver["adjustment"] == "qfq")
            & (silver["trade_date"] <= trade_date)
            & (silver["quality_status"] == "PASS")
        ].copy()
        differences: list[float] = []
        for _, instrument in selected.groupby("instrument_id"):
            source_returns: list[float] = []
            for _, source in instrument.groupby("source_id"):
                ordered = source.sort_values("trade_date").tail(20)
                if len(ordered) >= 20:
                    first = float(ordered.iloc[0]["close"])
                    last = float(ordered.iloc[-1]["close"])
                    source_returns.append(last / first - 1.0)
            if len(source_returns) >= 2:
                differences.append(max(source_returns) - min(source_returns))
            else:
                differences.append(1.0)
        return max(differences, default=1.0)

    def backfill(
        self,
        start: date,
        end: date,
        instruments: Sequence[str] | None = None,
    ) -> PipelineRun:
        """按目标日期成员范围回填；显式 instruments 只覆盖当前任务。"""

        enforce = instruments is None and self.config.collection.scope == "full_market"
        return self._execute(
            "backfill", start, end, instruments, enforce_full_market_gate=enforce
        )

    @staticmethod
    def _row_to_source_bar(row: Mapping[str, Any]) -> SourceBar:
        amount = row.get("amount")
        turnover = row.get("turnover_rate")
        pre_close = row.get("pre_close")
        return SourceBar(
            instrument_id=str(row["instrument_id"]),
            trade_date=pd.Timestamp(row["trade_date"]).date(),
            adjustment=BarAdjustment(str(row["adjustment"])),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
            amount=None if pd.isna(amount) else float(amount),
            pre_close=None if pd.isna(pre_close) else float(pre_close),
            turnover_rate=None if pd.isna(turnover) else float(turnover),
            source_id=str(row["source_id"]),
            upstream_domain=str(row["upstream_domain"]),
            available_at=pd.Timestamp(row["available_at"]).to_pydatetime(),
            raw_hash=str(row["raw_hash"]),
            adjustment_date=pd.Timestamp(row["adjustment_date"]).date(),
            adjustment_version=str(row["adjustment_version"]),
            trading_status=TradingStatus(
                str(row.get("trading_status") or TradingStatus.NORMAL.value)
            ),
            up_limit=(None if pd.isna(row.get("up_limit")) else float(row["up_limit"])),
            down_limit=(
                None if pd.isna(row.get("down_limit")) else float(row["down_limit"])
            ),
            is_st=bool(row.get("is_st", False)),
        )

    def reconcile(self, trade_date: date) -> PipelineRun:
        """只使用已持久化 Bronze 重算共识，不重新请求上游。"""

        frame = self.store.read_dataset("bronze", "etf_daily_bar")
        if frame.empty:
            raise RuntimeError("Bronze 为空，不能 reconcile")
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        selected = frame.loc[frame["trade_date"] == trade_date]
        if selected.empty:
            raise RuntimeError(f"Bronze 中没有 {trade_date} 数据")
        bars = [
            self._row_to_source_bar({str(key): value for key, value in row.items()})
            for row in selected.to_dict("records")
        ]
        hashes = sorted({bar.raw_hash for bar in bars})
        data_version = self._data_version("reconcile", trade_date, hashes)
        run_id = self._run_id("reconcile", trade_date)
        registered_version: str | None = None
        with self.store.writer_lock():
            self.store.start_run(run_id, "reconcile", trade_date, data_version)
            try:
                existing_version = data_version in self.store.available_versions()
                if not existing_version:
                    self.store.begin_data_version(
                        data_version,
                        parent_data_version=self.store.latest_ready_data_version(),
                        command="reconcile",
                    )
                    registered_version = data_version
                fusion = fuse_gold_bars(
                    bars,
                    data_version=data_version,
                    min_independent_sources=self.config.quality.min_independent_sources,
                )
                self.store.append_silver(bars, data_version, fusion.quality_by_key)
                published = self.store.append_bars(fusion.gold_bars)
                self.evaluate_research_readiness(trade_date, data_version)
                if not existing_version:
                    self.store.finish_data_version(data_version, "READY")
                self.store.finish_run(run_id, "SUCCESS", f"gold={published}")
            except Exception as exc:
                if registered_version is not None:
                    self.store.finish_data_version(registered_version, "FAILED")
                self.store.finish_run(run_id, "FAILED", str(exc))
                raise
        return PipelineRun(
            run_id, "reconcile", trade_date, data_version, published, "SUCCESS"
        )

    def publish(self, trade_date: date, data_version: str | None = None) -> PipelineRun:
        """仅验证既有门禁；禁止为“发布成功”而隐式补跑采集。"""

        status = self.registry.get(
            ReadinessGate.DAILY_MARKET_READY, trade_date, data_version
        )
        require_readiness(status)
        run_id = self._run_id("publish", trade_date)
        self.store.start_run(run_id, "publish", trade_date, status.data_version)
        self.store.finish_run(run_id, "SUCCESS", f"gate={status.state.value}")
        return PipelineRun(
            run_id,
            "publish",
            trade_date,
            status.data_version,
            status.published_count,
            status.state.value,
        )

    def compact(self) -> int:
        with self.store.writer_lock():
            return self.store.compact()

    def status(self, limit: int = 10) -> list[dict[str, str]]:
        return self.store.status(limit)

"""Parquet + DuckDB 数据存储门面。

写端只供数据管道使用；研究层通过 :class:`GoldDataReader` 锁定版本读取。
所有分区采用 staging + ``os.replace`` 原子替换，Gold 完整主键不可覆盖。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import duckdb
import pandas as pd
import psycopg
from filelock import FileLock

from quant_trading.data.providers import FetchResult, InstrumentRecord, SourceBar
from quant_trading.models import BarAdjustment, MarketBar


@dataclass(frozen=True, slots=True)
class RawArtifact:
    path: Path
    sha256: str
    size_bytes: int


class GoldDataReader(Protocol):
    def get_bars(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        *,
        adjustment: BarAdjustment,
        data_version: str,
    ) -> Sequence[MarketBar]: ...

    def available_versions(self) -> tuple[str, ...]: ...


class GoldDataWriter(Protocol):
    def append_bars(self, bars: Sequence[MarketBar]) -> int: ...


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"无法 JSON 序列化: {type(value).__name__}")


def _coerce_date(value: object) -> date:
    """兼容 Parquet 的 date、datetime 与 ISO 字符串，包括 9999-12-31。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class ParquetDuckDBStore:
    """服务器写入、研究机只读均可使用的本地列式存储。"""

    def __init__(
        self,
        data_root: Path,
        *,
        read_only: bool = False,
        min_free_gb: float = 1.0,
    ) -> None:
        self.data_root = data_root.resolve()
        self.catalog_path = self.data_root / "catalog" / "quant.duckdb"
        self.read_only = read_only
        self.min_free_gb = min_free_gb

    @property
    def lock_path(self) -> Path:
        return self.data_root / ".writer.lock"

    def bootstrap(self) -> tuple[Path, ...]:
        if self.read_only:
            raise PermissionError("只读存储不能 bootstrap")
        paths = tuple(
            self.data_root / name
            for name in ("raw", "bronze", "silver", "gold", "catalog", "staging")
        )
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            self._bootstrap_catalog(connection)
        return paths

    def writer_lock(self, timeout: float = 300.0) -> FileLock:
        if self.read_only:
            raise PermissionError("研究端禁止写入")
        self.data_root.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.lock_path), timeout=timeout)

    def ensure_free_space(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(self.data_root).free / 1024**3
        if free_gb < self.min_free_gb:
            raise RuntimeError(
                f"DISK_SPACE_LOW: 剩余 {free_gb:.2f} GiB，"
                f"要求 >= {self.min_free_gb:.2f} GiB"
            )

    def connect(self) -> duckdb.DuckDBPyConnection:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(self.catalog_path), read_only=self.read_only)

    @staticmethod
    def _bootstrap_catalog(connection: duckdb.DuckDBPyConnection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS etl_run (
                run_id VARCHAR PRIMARY KEY,
                command VARCHAR NOT NULL,
                trade_date DATE,
                data_version VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ,
                detail VARCHAR
            );
            CREATE TABLE IF NOT EXISTS data_readiness (
                gate VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                data_version VARCHAR NOT NULL,
                state VARCHAR NOT NULL,
                coverage DOUBLE NOT NULL,
                published_count BIGINT NOT NULL,
                expected_count BIGINT NOT NULL,
                blocking_issues VARCHAR NOT NULL,
                warnings VARCHAR NOT NULL,
                evaluated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (gate, trade_date, data_version)
            );
            CREATE TABLE IF NOT EXISTS quality_issue (
                run_id VARCHAR NOT NULL,
                instrument_id VARCHAR,
                trade_date DATE,
                adjustment VARCHAR,
                issue_code VARCHAR NOT NULL,
                severity VARCHAR NOT NULL,
                details VARCHAR NOT NULL,
                sources VARCHAR NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshot_audit (
                snapshot_id VARCHAR PRIMARY KEY,
                profile VARCHAR NOT NULL,
                data_version VARCHAR NOT NULL,
                manifest_sha256 VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS data_version_lineage (
                data_version VARCHAR PRIMARY KEY,
                parent_data_version VARCHAR,
                command VARCHAR NOT NULL,
                state VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            """
        )

    @staticmethod
    def _catalog_table_exists(
        connection: duckdb.DuckDBPyConnection, table_name: str
    ) -> bool:
        row = connection.execute(
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema='main' AND table_name=?
            """,
            [table_name],
        ).fetchone()
        return row is not None and int(row[0]) > 0

    def latest_ready_data_version(self) -> str | None:
        """返回最近完成的逻辑数据发布版本，而不是按字符串排序猜测。"""
        if not self.catalog_path.exists():
            return None
        with self.connect() as connection:
            if not self._catalog_table_exists(connection, "data_version_lineage"):
                return None
            row = connection.execute(
                """
                SELECT data_version FROM data_version_lineage
                WHERE state = 'READY'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        return str(row[0]) if row is not None else None

    def begin_data_version(
        self,
        data_version: str,
        *,
        parent_data_version: str | None,
        command: str,
    ) -> None:
        """登记不可变发布的父版本；同一版本重复运行必须保持相同父链。"""
        if self.read_only:
            raise PermissionError("研究端禁止登记数据版本")
        with self.connect() as connection:
            self._bootstrap_catalog(connection)
            existing = connection.execute(
                """
                SELECT parent_data_version, command FROM data_version_lineage
                WHERE data_version = ?
                """,
                [data_version],
            ).fetchone()
            if existing is not None:
                existing_parent = None if existing[0] is None else str(existing[0])
                if (
                    existing_parent != parent_data_version
                    or str(existing[1]) != command
                ):
                    raise RuntimeError(f"数据版本 {data_version} 的谱系定义发生变化")
                connection.execute(
                    "UPDATE data_version_lineage SET state='PENDING' "
                    "WHERE data_version=?",
                    [data_version],
                )
                return
            connection.execute(
                "INSERT INTO data_version_lineage VALUES (?, ?, ?, 'PENDING', ?)",
                [data_version, parent_data_version, command, datetime.now(UTC)],
            )

    def finish_data_version(self, data_version: str, state: str) -> None:
        """把逻辑发布标记为 READY 或 FAILED。"""
        if state not in {"READY", "FAILED"}:
            raise ValueError("数据版本状态只能是 READY/FAILED")
        with self.connect() as connection:
            connection.execute(
                "UPDATE data_version_lineage SET state=? WHERE data_version=?",
                [state, data_version],
            )

    def version_chain(
        self, data_version: str, *, include_pending: bool = False
    ) -> tuple[str, ...]:
        """从当前版本向父版本展开；首项优先级最高。"""
        if not data_version:
            raise ValueError("data_version 不能为空")
        if not self.catalog_path.exists():
            return (data_version,)
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = data_version
        with self.connect() as connection:
            if not self._catalog_table_exists(connection, "data_version_lineage"):
                return (data_version,)
            while current is not None:
                if current in seen:
                    raise RuntimeError(f"数据版本谱系存在环: {current}")
                seen.add(current)
                row = connection.execute(
                    """
                    SELECT parent_data_version, state FROM data_version_lineage
                    WHERE data_version = ?
                    """,
                    [current],
                ).fetchone()
                if row is None:
                    # 兼容谱系功能加入前已经存在的不可变物理版本。
                    chain.append(current)
                    break
                state = str(row[1])
                if state != "READY" and not (
                    include_pending and current == data_version and state == "PENDING"
                ):
                    raise RuntimeError(f"数据版本 {current} 尚未完成发布: {state}")
                chain.append(current)
                current = None if row[0] is None else str(row[0])
        return tuple(chain)

    def resolve_versioned_dataset(
        self,
        layer: str,
        dataset: str,
        data_version: str,
        *,
        business_key: Sequence[str],
        include_pending: bool = False,
    ) -> pd.DataFrame:
        """按版本谱系合成一个逻辑快照，新版本的业务键覆盖父版本。"""
        frame = self.read_dataset(layer, dataset)
        if frame.empty or "data_version" not in frame:
            return frame
        chain = self.version_chain(data_version, include_pending=include_pending)
        priority = {version: index for index, version in enumerate(chain)}
        selected = frame.loc[frame["data_version"].astype(str).isin(priority)].copy()
        if selected.empty:
            return selected
        selected["_version_priority"] = (
            selected["data_version"].astype(str).map(priority)
        )
        selected = selected.sort_values("_version_priority")
        selected = selected.drop_duplicates(list(business_key), keep="first")
        selected = selected.drop(columns="_version_priority")
        # 对研究消费者暴露请求的逻辑发布版本，底层物理来源仍可由谱系审计。
        selected["data_version"] = data_version
        return selected.reset_index(drop=True)

    def save_raw(self, result: FetchResult, run_id: str) -> RawArtifact:
        if self.read_only:
            raise PermissionError("研究端禁止保存 Raw")
        fetched = result.fetched_at.astimezone(UTC)
        target_dir = (
            self.data_root
            / "raw"
            / result.source_id.replace("/", "_")
            / result.dataset
            / fetched.strftime("%Y-%m-%d")
            / run_id
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        digest = result.raw_hash
        target = target_dir / f"{digest[:20]}.json.gz"
        if not target.exists():
            staging = self.data_root / "staging" / f"{uuid4().hex}.gz"
            staging.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(staging, "wb", compresslevel=6) as handle:
                handle.write(result.content)
            os.replace(staging, target)
        metadata = {
            "source_id": result.source_id,
            "upstream_domain": result.upstream_domain,
            "dataset": result.dataset,
            "fetched_at": result.fetched_at,
            "status_code": result.status_code,
            "state": result.state.value,
            "error_detail": result.error_detail,
            "content_type": result.content_type,
            "raw_hash": digest,
            "request_params": dict(result.request_params),
        }
        meta_target = target.with_suffix(target.suffix + ".meta.json")
        meta_content = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
        staging_meta = self.data_root / "staging" / f"{uuid4().hex}.json"
        staging_meta.write_text(meta_content, encoding="utf-8")
        os.replace(staging_meta, meta_target)
        return RawArtifact(target, digest, target.stat().st_size)

    def partition_path(self, layer: str, dataset: str, year: int, month: int) -> Path:
        return (
            self.data_root
            / layer
            / dataset
            / f"year={year:04d}"
            / f"month={month:02d}"
            / "part.parquet"
        )

    def dataset_files(self, layer: str, dataset: str) -> tuple[Path, ...]:
        root = self.data_root / layer / dataset
        if not root.exists():
            return ()
        return tuple(sorted(root.glob("year=*/month=*/part.parquet")))

    def read_dataset(self, layer: str, dataset: str) -> pd.DataFrame:
        files = self.dataset_files(layer, dataset)
        if not files:
            return pd.DataFrame()
        return pd.concat((pd.read_parquet(path) for path in files), ignore_index=True)

    def _write_partitioned(
        self,
        layer: str,
        dataset: str,
        frame: pd.DataFrame,
        *,
        date_col: str,
        key_cols: Sequence[str],
        immutable: bool,
    ) -> int:
        if self.read_only:
            raise PermissionError("研究端禁止写入")
        if frame.empty:
            return 0
        work = frame.copy()
        work[date_col] = pd.to_datetime(work[date_col], errors="raise")
        changed = 0
        grouped = work.groupby([work[date_col].dt.year, work[date_col].dt.month])
        for (year, month), partition in grouped:
            target = self.partition_path(
                layer, dataset, int(str(year)), int(str(month))
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            existing = pd.read_parquet(target) if target.exists() else pd.DataFrame()
            if immutable and not existing.empty:
                overlap = existing.merge(partition, on=list(key_cols), how="inner")
                if not overlap.empty:
                    existing_keys = existing.set_index(list(key_cols))
                    incoming_keys = partition.set_index(list(key_cols))
                    common = existing_keys.index.intersection(incoming_keys.index)
                    compare_columns = sorted(
                        (set(existing_keys.columns) & set(incoming_keys.columns))
                        - {"published_at"}
                    )
                    left = (
                        existing_keys.loc[common, compare_columns]
                        .fillna("<NA>")
                        .astype(str)
                    )
                    right = (
                        incoming_keys.loc[common, compare_columns]
                        .fillna("<NA>")
                        .astype(str)
                    )
                    if not left.equals(right):
                        raise RuntimeError(
                            f"Gold 不可覆盖: {dataset} 有 {len(common)} 个主键"
                            "内容发生变化"
                        )
            combined = pd.concat([existing, partition], ignore_index=True, sort=False)
            before = len(existing)
            combined = combined.drop_duplicates(
                list(key_cols), keep="first" if immutable else "last"
            )
            combined = combined.sort_values(list(key_cols)).reset_index(drop=True)
            staging = self.data_root / "staging" / f"{uuid4().hex}.parquet"
            staging.parent.mkdir(parents=True, exist_ok=True)
            combined.to_parquet(staging, index=False, compression="zstd")
            os.replace(staging, target)
            changed += max(len(combined) - before, 0)
        return changed

    @staticmethod
    def _source_frame(bars: Sequence[SourceBar], data_version: str) -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        for bar in bars:
            row = asdict(bar)
            row["adjustment"] = bar.adjustment.value
            row["data_version"] = data_version
            row["schema_version"] = "1.0.0"
            rows.append(row)
        return pd.DataFrame(rows)

    def upsert_bronze(
        self,
        bars: Sequence[SourceBar],
        data_version: str,
        *,
        dataset: str = "etf_daily_bar",
    ) -> int:
        frame = self._source_frame(bars, data_version)
        return self._write_partitioned(
            "bronze",
            dataset,
            frame,
            date_col="trade_date",
            key_cols=("instrument_id", "trade_date", "adjustment", "source_id"),
            immutable=False,
        )

    def append_silver(
        self,
        bars: Sequence[SourceBar],
        data_version: str,
        quality_by_key: Mapping[tuple[str, date, str, str], tuple[str, str]],
        *,
        dataset: str = "etf_daily_bar",
    ) -> int:
        frame = self._source_frame(bars, data_version)
        if frame.empty:
            return 0
        statuses: list[str] = []
        details: list[str] = []
        for row in frame.to_dict("records"):
            key = (
                str(row["instrument_id"]),
                pd.Timestamp(row["trade_date"]).date(),
                str(row["adjustment"]),
                str(row["source_id"]),
            )
            status, detail = quality_by_key.get(key, ("QUARANTINED", "未执行校验"))
            statuses.append(status)
            details.append(detail)
        frame["quality_status"] = statuses
        frame["conflict_detail"] = details
        return self._write_partitioned(
            "silver",
            dataset,
            frame,
            date_col="trade_date",
            key_cols=(
                "instrument_id",
                "trade_date",
                "adjustment",
                "source_id",
                "data_version",
            ),
            immutable=True,
        )

    def append_bars(self, bars: Sequence[MarketBar]) -> int:
        return self._append_market_bars("etf_daily_bar", bars)

    def append_index_bars(self, bars: Sequence[MarketBar]) -> int:
        return self._append_market_bars("index_daily", bars)

    def append_stock_bars(self, bars: Sequence[MarketBar]) -> int:
        return self._append_market_bars("stock_daily_bar", bars)

    def _append_market_bars(self, dataset: str, bars: Sequence[MarketBar]) -> int:
        rows: list[dict[str, object]] = []
        for bar in bars:
            row = bar.model_dump(mode="json")
            row["source_map"] = json.dumps(
                bar.source_map, ensure_ascii=False, sort_keys=True
            )
            row["quality_flags"] = json.dumps(
                list(bar.quality_flags), ensure_ascii=False
            )
            rows.append(row)
        frame = pd.DataFrame(rows)
        changed = self._write_partitioned(
            "gold",
            dataset,
            frame,
            date_col="trade_date",
            key_cols=("instrument_id", "trade_date", "adjustment", "data_version"),
            immutable=True,
        )
        self.refresh_views()
        return changed

    def append_trade_calendar(
        self,
        rows: Sequence[Mapping[str, object]],
    ) -> int:
        frame = pd.DataFrame(rows)
        changed = self._write_partitioned(
            "gold",
            "trade_calendar",
            frame,
            date_col="calendar_date",
            key_cols=("calendar_date", "data_version"),
            immutable=True,
        )
        self.refresh_views()
        return changed

    def append_features(self, rows: Sequence[Mapping[str, object]]) -> int:
        """追加同一行情版本派生的 ETF 日频特征。"""
        frame = pd.DataFrame(rows)
        changed = self._write_partitioned(
            "gold",
            "etf_features_daily",
            frame,
            date_col="trade_date",
            key_cols=("instrument_id", "trade_date", "data_version"),
            immutable=True,
        )
        self.refresh_views()
        return changed

    def append_sector_constituents(
        self, rows: Sequence[Mapping[str, object]], data_version: str
    ) -> int:
        """追加一份可追溯的板块成分快照，不允许覆盖既有历史。"""
        normalized: list[dict[str, object]] = []
        published_at = datetime.now(UTC)
        for raw in rows:
            snapshot_date = _coerce_date(raw["snapshot_date"])
            board_type = str(raw["board_type"]).strip().lower()
            if board_type not in {"concept", "industry"}:
                raise ValueError("board_type 只能是 concept/industry")
            board_name = str(raw["board_name"]).strip()
            instrument_id = (
                str(raw.get("instrument_id") or raw.get("ts_code") or "")
                .strip()
                .upper()
            )
            source_id = str(raw.get("source_id") or "").strip()
            if not board_name or not instrument_id or not source_id:
                raise ValueError(
                    "板块快照的 board_name/instrument_id/source_id 不能为空"
                )
            normalized.append(
                {
                    "snapshot_date": snapshot_date,
                    "board_type": board_type,
                    "board_name": board_name,
                    "instrument_id": instrument_id,
                    "source_id": source_id,
                    "source_file_sha256": str(raw.get("source_file_sha256") or ""),
                    "data_version": data_version,
                    "published_at": published_at,
                    "schema_version": "1.0.0",
                }
            )
        frame = pd.DataFrame(normalized)
        changed = self._write_partitioned(
            "gold",
            "sector_constituent_snapshot",
            frame,
            date_col="snapshot_date",
            key_cols=(
                "snapshot_date",
                "board_type",
                "board_name",
                "instrument_id",
                "data_version",
            ),
            immutable=True,
        )
        self.refresh_views()
        return changed

    def append_master(
        self, records: Sequence[InstrumentRecord], data_version: str
    ) -> int:
        """融合双源完整快照，并以新版本写出可回放的 SCD2 状态。"""
        if not records:
            return 0
        effective_dates = {record.effective_date for record in records}
        if len(effective_dates) != 1:
            raise ValueError("同一次 ETF master 发布只能包含一个 effective_date")
        effective_date = next(iter(effective_dates))
        grouped: dict[str, list[InstrumentRecord]] = {}
        for record in records:
            grouped.setdefault(record.instrument_id, []).append(record)
        primary = {
            instrument: next(
                (
                    record
                    for record in items
                    if record.source_id == "eastmoney_etf_snapshot"
                ),
                items[0],
            )
            for instrument, items in grouped.items()
            if any(record.source_id == "eastmoney_etf_snapshot" for record in items)
        }
        if not primary:
            raise RuntimeError("ETF master 缺少东方财富主清单")
        missing_consensus = sorted(
            instrument
            for instrument, record in primary.items()
            if len(
                {
                    item.upstream_domain
                    for item in grouped[instrument]
                    if item.instrument_id == record.instrument_id
                }
            )
            < 2
        )
        if missing_consensus:
            sample = ", ".join(missing_consensus[:10])
            raise RuntimeError(
                f"ETF master 有 {len(missing_consensus)} 个成员缺少双源共识: {sample}"
            )

        existing = self.read_dataset("gold", "etf_master_scd")
        previous = pd.DataFrame()
        if not existing.empty:
            existing["effective_date"] = existing["effective_date"].map(_coerce_date)
            eligible = existing.loc[existing["effective_date"] <= effective_date]
            if not eligible.empty:
                latest_effective = eligible["effective_date"].max()
                eligible = eligible.loc[eligible["effective_date"] == latest_effective]
                latest_published = pd.to_datetime(eligible["published_at"]).max()
                previous = eligible.loc[
                    pd.to_datetime(eligible["published_at"]) == latest_published
                ]
        previous_by_id = {
            str(row["instrument_id"]): row
            for row in previous.to_dict("records")
            if bool(row.get("is_current"))
        }

        published_at = datetime.now(UTC)
        rows: list[dict[str, object]] = []
        for instrument, record in sorted(primary.items()):
            items = grouped[instrument]
            prior = previous_by_id.get(instrument)
            unchanged = prior is not None and all(
                str(prior.get(field) or "") == str(getattr(record, field) or "")
                for field in ("code", "exchange", "name", "listing_date")
            )
            valid_from = (
                _coerce_date(prior["valid_from"])
                if unchanged and prior is not None
                else effective_date
            )
            rows.append(
                {
                    "instrument_id": record.instrument_id,
                    "code": record.code,
                    "exchange": record.exchange,
                    "name": record.name,
                    "listing_date": record.listing_date,
                    "effective_date": effective_date,
                    "valid_from": valid_from,
                    "valid_to": date(9999, 12, 31),
                    "is_current": True,
                    "delisting_date": None,
                    "source_count": len({item.upstream_domain for item in items}),
                    "source_map": json.dumps(
                        {item.source_id: True for item in items},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "available_at": max(item.available_at for item in items),
                    "data_version": data_version,
                    "published_at": published_at,
                    "schema_version": "1.0.0",
                }
            )
        for instrument, prior in sorted(previous_by_id.items()):
            if instrument in primary:
                continue
            valid_from = _coerce_date(prior["valid_from"])
            if valid_from >= effective_date:
                continue
            carried: dict[str, object] = {
                str(key): value for key, value in prior.items()
            }
            carried.update(
                {
                    "effective_date": effective_date,
                    "valid_from": valid_from,
                    "valid_to": effective_date - timedelta(days=1),
                    "is_current": False,
                    "delisting_date": effective_date,
                    "data_version": data_version,
                    "published_at": published_at,
                }
            )
            rows.append(carried)
        frame = pd.DataFrame(rows)
        if frame.empty:
            return 0
        return self._write_partitioned(
            "gold",
            "etf_master_scd",
            frame,
            date_col="valid_from",
            key_cols=("instrument_id", "valid_from", "data_version"),
            immutable=True,
        )

    def get_universe(
        self, as_of: date, data_version: str | None = None
    ) -> tuple[str, ...]:
        eligible = self._universe_frame(as_of, data_version)
        return tuple(sorted(eligible["instrument_id"].astype(str)))

    def get_universe_members(
        self, as_of: date, data_version: str | None = None
    ) -> dict[str, date | None]:
        eligible = self._universe_frame(as_of, data_version)
        result: dict[str, date | None] = {}
        for row in eligible.to_dict("records"):
            raw_listing = row.get("listing_date")
            listing = (
                None
                if raw_listing is None or pd.isna(raw_listing)
                else pd.Timestamp(raw_listing).date()
            )
            result[str(row["instrument_id"])] = listing
        return result

    def get_universe_for_range(self, start: date, end: date) -> tuple[str, ...]:
        """返回区间内曾有效的 ETF，并拒绝用晚于起点的清单回填历史。"""
        if start > end:
            raise ValueError("start 不能晚于 end")
        frame = self.read_dataset("gold", "etf_master_scd")
        if frame.empty:
            raise RuntimeError("etf_master_scd 为空，无法执行 point-in-time 回填")
        for column in ("effective_date", "valid_from", "valid_to"):
            frame[column] = frame[column].map(_coerce_date)
        snapshots = sorted(set(frame["effective_date"]))
        baseline = [item for item in snapshots if item <= start]
        if not baseline:
            earliest = min(snapshots)
            raise RuntimeError(
                "ETF master 历史覆盖不足："
                f"回填起点 {start} 早于首个清单快照 {earliest}；"
                "拒绝使用当前成分替代历史成分"
            )
        effective_dates = [max(baseline)] + [
            item for item in snapshots if start < item <= end
        ]
        instruments: set[str] = set()
        for effective_date in effective_dates:
            snapshot = frame.loc[frame["effective_date"] == effective_date].copy()
            if "published_at" in snapshot:
                latest = pd.to_datetime(snapshot["published_at"], utc=True).max()
                snapshot = snapshot.loc[
                    pd.to_datetime(snapshot["published_at"], utc=True) == latest
                ]
            active = snapshot.loc[
                snapshot["is_current"].astype(bool)
                & (snapshot["valid_from"] <= effective_date)
                & (snapshot["valid_to"] >= effective_date)
            ]
            instruments.update(active["instrument_id"].astype(str))
        if not instruments:
            raise RuntimeError(f"{start} 至 {end} 的 point-in-time ETF 范围为空")
        return tuple(sorted(instruments))

    def _universe_frame(self, as_of: date, data_version: str | None) -> pd.DataFrame:
        frame = self.read_dataset("gold", "etf_master_scd")
        if frame.empty:
            return frame
        for column in ("effective_date", "valid_from", "valid_to"):
            frame[column] = frame[column].map(_coerce_date)
        mask = frame["effective_date"] <= as_of
        if "listing_date" in frame:
            listing = pd.to_datetime(frame["listing_date"], errors="coerce").dt.date
            mask &= listing.isna() | (listing <= as_of)
        if data_version is not None:
            mask &= frame["data_version"] == data_version
        eligible = frame.loc[mask].copy()
        if eligible.empty:
            return eligible
        # 每个 data_version 都保存一份完整 SCD 状态；先锁定当时已发布的最近快照，
        # 再按有效区间取成员，避免混入后来才知道的退市信息。
        latest_effective = eligible["effective_date"].max()
        eligible = eligible.loc[eligible["effective_date"] == latest_effective]
        if data_version is None and "published_at" in eligible:
            latest_published = pd.to_datetime(eligible["published_at"]).max()
            eligible = eligible.loc[
                pd.to_datetime(eligible["published_at"]) == latest_published
            ]
        eligible = eligible.loc[
            eligible["is_current"].astype(bool)
            & (eligible["valid_from"] <= as_of)
            & (eligible["valid_to"] >= as_of)
        ].drop_duplicates("instrument_id", keep="last")
        return pd.DataFrame(eligible)

    def get_bars(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        *,
        adjustment: BarAdjustment,
        data_version: str,
    ) -> Sequence[MarketBar]:
        return self._get_market_bars(
            "etf_daily_bar",
            instruments,
            start,
            end,
            adjustment=adjustment,
            data_version=data_version,
        )

    def get_bars_during_publish(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        *,
        adjustment: BarAdjustment,
        data_version: str,
    ) -> Sequence[MarketBar]:
        """写事务内读取 PENDING 逻辑版本，仅供派生数据和门禁计算。"""
        if self.read_only:
            raise PermissionError("只读研究端不能读取未完成发布")
        return self._get_market_bars(
            "etf_daily_bar",
            instruments,
            start,
            end,
            adjustment=adjustment,
            data_version=data_version,
            include_pending=True,
        )

    def get_index_bars_during_publish(
        self,
        instrument: str,
        start: date,
        end: date,
        *,
        data_version: str,
    ) -> Sequence[MarketBar]:
        """写事务内读取 PENDING 基准版本。"""
        if self.read_only:
            raise PermissionError("只读研究端不能读取未完成发布")
        return self._get_market_bars(
            "index_daily",
            (instrument,),
            start,
            end,
            adjustment=BarAdjustment.RAW,
            data_version=data_version,
            include_pending=True,
        )

    def get_index_bars(
        self,
        instrument: str,
        start: date,
        end: date,
        *,
        data_version: str,
    ) -> Sequence[MarketBar]:
        return self._get_market_bars(
            "index_daily",
            (instrument,),
            start,
            end,
            adjustment=BarAdjustment.RAW,
            data_version=data_version,
        )

    def get_stock_bars(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        *,
        adjustment: BarAdjustment,
        data_version: str,
    ) -> Sequence[MarketBar]:
        return self._get_market_bars(
            "stock_daily_bar",
            instruments,
            start,
            end,
            adjustment=adjustment,
            data_version=data_version,
        )

    def get_theme_members(
        self,
        as_of: date,
        *,
        data_version: str,
        board_types: Sequence[str] = (),
        themes: Sequence[str] = (),
    ) -> dict[str, tuple[str, ...]]:
        """读取 as_of 当时最近一份板块快照，绝不回填未来成分。"""
        frame = self.resolve_versioned_dataset(
            "gold",
            "sector_constituent_snapshot",
            data_version,
            business_key=(
                "snapshot_date",
                "board_type",
                "board_name",
                "instrument_id",
            ),
        )
        if frame.empty:
            raise RuntimeError("sector_constituent_snapshot 为空，主题策略不可运行")
        frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"]).dt.date
        eligible = frame.loc[frame["snapshot_date"] <= as_of].copy()
        if eligible.empty:
            earliest = min(frame["snapshot_date"])
            raise RuntimeError(
                f"{as_of} 之前没有板块成分快照（最早 {earliest}）；"
                "拒绝使用当前成分替代历史成分"
            )
        latest = max(eligible["snapshot_date"])
        eligible = eligible.loc[eligible["snapshot_date"] == latest]
        if board_types:
            eligible = eligible.loc[
                eligible["board_type"].isin({item.lower() for item in board_types})
            ]
        if themes:
            eligible = eligible.loc[eligible["board_name"].isin(set(themes))]
        result: dict[str, tuple[str, ...]] = {}
        for (board_type, board_name), group in eligible.groupby(
            ["board_type", "board_name"]
        ):
            result[f"{board_type}:{board_name}"] = tuple(
                sorted(set(group["instrument_id"].astype(str)))
            )
        if not result:
            raise RuntimeError(f"{latest} 的板块快照在配置过滤后为空")
        return result

    def get_trade_calendar(
        self, start: date, end: date, *, data_version: str
    ) -> tuple[date, ...]:
        frame = self.resolve_versioned_dataset(
            "gold",
            "trade_calendar",
            data_version,
            business_key=("calendar_date",),
        )
        if frame.empty:
            return ()
        frame["calendar_date"] = pd.to_datetime(frame["calendar_date"]).dt.date
        selected = frame.loc[
            frame["calendar_date"].between(start, end)
            & frame["is_trade_day"].astype(bool)
        ]
        return tuple(sorted(set(selected["calendar_date"])))

    def get_next_trade_day(self, after: date, *, data_version: str) -> date | None:
        frame = self.resolve_versioned_dataset(
            "gold",
            "trade_calendar",
            data_version,
            business_key=("calendar_date",),
        )
        if frame.empty:
            return None
        frame["calendar_date"] = pd.to_datetime(frame["calendar_date"]).dt.date
        selected = frame.loc[
            (frame["calendar_date"] > after) & frame["is_trade_day"].astype(bool)
        ]["calendar_date"]
        return min(selected) if not selected.empty else None

    def _get_market_bars(
        self,
        dataset: str,
        instruments: Sequence[str],
        start: date,
        end: date,
        *,
        adjustment: BarAdjustment,
        data_version: str,
        include_pending: bool = False,
    ) -> Sequence[MarketBar]:
        frame = self.resolve_versioned_dataset(
            "gold",
            dataset,
            data_version,
            business_key=("instrument_id", "trade_date", "adjustment"),
            include_pending=include_pending,
        )
        if frame.empty:
            return ()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        mask = (
            frame["instrument_id"].isin(set(instruments))
            & frame["trade_date"].between(start, end)
            & (frame["adjustment"] == adjustment.value)
        )
        result: list[MarketBar] = []
        for row in (
            frame.loc[mask]
            .sort_values(["instrument_id", "trade_date"])
            .to_dict("records")
        ):
            row["source_map"] = json.loads(str(row.get("source_map") or "{}"))
            row["quality_flags"] = tuple(
                json.loads(str(row.get("quality_flags") or "[]"))
            )
            for nullable in (
                "pre_close",
                "turnover_rate",
                "up_limit",
                "down_limit",
                "adjustment_source",
                "adjustment_date",
                "adjustment_version",
                "published_at",
            ):
                if nullable in row and pd.isna(row[nullable]):
                    row[nullable] = None
            result.append(MarketBar.model_validate(row))
        return tuple(result)

    def available_versions(self) -> tuple[str, ...]:
        if self.catalog_path.exists():
            with self.connect() as connection:
                rows = (
                    connection.execute(
                        """
                        SELECT data_version FROM data_version_lineage
                        WHERE state='READY' ORDER BY created_at
                        """
                    ).fetchall()
                    if self._catalog_table_exists(connection, "data_version_lineage")
                    else []
                )
            if rows:
                return tuple(str(row[0]) for row in rows)
        frame = self.read_dataset("gold", "etf_daily_bar")
        if frame.empty or "data_version" not in frame:
            return ()
        if "published_at" in frame:
            frame["published_at"] = pd.to_datetime(frame["published_at"], utc=True)
            frame = frame.sort_values("published_at")
        return tuple(dict.fromkeys(frame["data_version"].astype(str)))

    def refresh_views(self) -> None:
        if self.read_only or not self.catalog_path.exists():
            return
        with self.connect() as connection:
            self._bootstrap_catalog(connection)
            for view, layer, dataset, business_key in (
                (
                    "v_etf_daily_verified",
                    "gold",
                    "etf_daily_bar",
                    ("instrument_id", "trade_date", "adjustment"),
                ),
                (
                    "v_index_daily_verified",
                    "gold",
                    "index_daily",
                    ("instrument_id", "trade_date", "adjustment"),
                ),
                (
                    "v_stock_daily_verified",
                    "gold",
                    "stock_daily_bar",
                    ("instrument_id", "trade_date", "adjustment"),
                ),
                (
                    "v_etf_universe_pit",
                    "gold",
                    "etf_master_scd",
                    ("instrument_id", "valid_from"),
                ),
                (
                    "v_trade_calendar",
                    "gold",
                    "trade_calendar",
                    ("calendar_date",),
                ),
                (
                    "v_etf_features_daily",
                    "gold",
                    "etf_features_daily",
                    ("instrument_id", "trade_date"),
                ),
                (
                    "v_sector_constituent_snapshot",
                    "gold",
                    "sector_constituent_snapshot",
                    ("snapshot_date", "board_type", "board_name", "instrument_id"),
                ),
                ("v_quality_silver", "silver", "etf_daily_bar", ()),
            ):
                files = self.dataset_files(layer, dataset)
                connection.execute(f"DROP VIEW IF EXISTS {view}")
                if files:
                    glob = str(
                        self.data_root
                        / layer
                        / dataset
                        / "year=*"
                        / "month=*"
                        / "part.parquet"
                    ).replace("'", "''")
                    if business_key:
                        partition = ", ".join(business_key)
                        connection.execute(
                            f"""
                            CREATE VIEW {view} AS
                            SELECT * EXCLUDE (_version_rank)
                            FROM (
                                SELECT *, row_number() OVER (
                                    PARTITION BY {partition}
                                    ORDER BY published_at DESC, data_version DESC
                                ) AS _version_rank
                                FROM read_parquet('{glob}')
                            )
                            WHERE _version_rank = 1
                            """
                        )
                    else:
                        connection.execute(
                            f"CREATE VIEW {view} AS "
                            f"SELECT * FROM read_parquet('{glob}')"
                        )
            connection.execute("CHECKPOINT")

    def start_run(
        self,
        run_id: str,
        command: str,
        trade_date: date | None,
        data_version: str,
    ) -> None:
        with self.connect() as connection:
            self._bootstrap_catalog(connection)
            connection.execute(
                "INSERT INTO etl_run VALUES (?, ?, ?, ?, 'RUNNING', ?, NULL, '')",
                [run_id, command, trade_date, data_version, datetime.now(UTC)],
            )

    def finish_run(self, run_id: str, status: str, detail: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE etl_run SET status=?, finished_at=?, detail=? WHERE run_id=?",
                [status, datetime.now(UTC), detail, run_id],
            )

    def update_run_version(self, run_id: str, data_version: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE etl_run SET data_version=? WHERE run_id=?",
                [data_version, run_id],
            )

    def record_quality_issues(
        self, run_id: str, issues: Sequence[Mapping[str, object]]
    ) -> None:
        if not issues:
            return
        rows = [
            (
                run_id,
                issue.get("instrument_id"),
                issue.get("trade_date"),
                issue.get("adjustment"),
                issue["issue_code"],
                issue["severity"],
                issue["detail"],
                json.dumps(issue.get("sources", ()), ensure_ascii=False),
                datetime.now(UTC),
            )
            for issue in issues
        ]
        with self.connect() as connection:
            connection.executemany(
                "INSERT INTO quality_issue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def status(self, limit: int = 20) -> list[dict[str, str]]:
        if not self.catalog_path.exists():
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, command, coalesce(cast(trade_date AS VARCHAR), ''),
                       data_version, status, coalesce(detail, '')
                FROM etl_run ORDER BY started_at DESC LIMIT ?
                """,
                [limit],
            ).fetchall()
        return [
            {
                "run_id": str(row[0]),
                "command": str(row[1]),
                "trade_date": str(row[2]),
                "data_version": str(row[3]),
                "status": str(row[4]),
                "detail": str(row[5]),
            }
            for row in rows
        ]

    def compact(self) -> int:
        if self.read_only:
            raise PermissionError("研究端禁止 compact")
        rewritten = 0
        for layer in ("bronze", "silver", "gold"):
            root = self.data_root / layer
            if not root.exists():
                continue
            for target in root.glob("*/year=*/month=*/part.parquet"):
                frame = pd.read_parquet(target)
                staging = self.data_root / "staging" / f"{uuid4().hex}.parquet"
                frame.to_parquet(staging, index=False, compression="zstd")
                os.replace(staging, target)
                rewritten += 1
        self.refresh_views()
        return rewritten


class PostgresAuditStore:
    """远程 PostgreSQL 的运行/门禁审计写入器，不承载本地行情扫描。"""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN 不能为空")
        self._dsn = dsn

    @property
    def dsn(self) -> str:
        """供显式迁移命令建立一个受控事务。"""
        return self._dsn

    def bootstrap(self) -> None:
        with psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS quant_data_readiness (
                    gate text NOT NULL,
                    trade_date date NOT NULL,
                    data_version text NOT NULL,
                    state text NOT NULL,
                    payload jsonb NOT NULL,
                    evaluated_at timestamptz NOT NULL,
                    PRIMARY KEY (gate, trade_date, data_version)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS quant_etl_run (
                    run_id text PRIMARY KEY,
                    command text NOT NULL,
                    trade_date date,
                    data_version text NOT NULL,
                    status text NOT NULL,
                    started_at timestamptz NOT NULL,
                    finished_at timestamptz,
                    detail text NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quant_quality_issue (
                    run_id text NOT NULL,
                    instrument_id text,
                    trade_date date,
                    adjustment text,
                    issue_code text NOT NULL,
                    severity text NOT NULL,
                    details text NOT NULL,
                    sources jsonb NOT NULL,
                    recorded_at timestamptz NOT NULL,
                    UNIQUE NULLS NOT DISTINCT
                      (run_id, instrument_id, trade_date, adjustment, issue_code)
                );
                CREATE TABLE IF NOT EXISTS quant_snapshot_audit (
                    snapshot_id text PRIMARY KEY,
                    profile text NOT NULL,
                    data_version text NOT NULL,
                    manifest_sha256 text NOT NULL,
                    created_at timestamptz NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quant_research_run (
                    run_id text PRIMARY KEY,
                    run_type text NOT NULL,
                    snapshot_id text NOT NULL,
                    data_version text NOT NULL,
                    payload jsonb NOT NULL,
                    created_at timestamptz NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quant_signal_daily (
                    signal_id text PRIMARY KEY,
                    run_id text NOT NULL REFERENCES quant_research_run(run_id),
                    signal_date date NOT NULL,
                    execution_date date NOT NULL,
                    instrument_id text NOT NULL,
                    strategy_name text NOT NULL,
                    strategy_version text NOT NULL,
                    data_version text NOT NULL,
                    payload jsonb NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quant_weekly_rotation_score (
                    run_id text NOT NULL REFERENCES quant_research_run(run_id),
                    signal_date date NOT NULL,
                    instrument_id text NOT NULL,
                    strategy_name text NOT NULL,
                    score double precision NOT NULL,
                    target_weight double precision NOT NULL,
                    payload jsonb NOT NULL,
                    PRIMARY KEY (run_id, signal_date, instrument_id, strategy_name)
                );
                """
            )
            connection.commit()

    def write_research_result(
        self, payload: Mapping[str, object], *, run_type: str
    ) -> None:
        """原子保存研究运行、正式信号和目标评分；重复 run_id 幂等。"""
        run_id = str(payload.get("run_id") or "")
        snapshot_id = str(payload.get("snapshot_id") or "")
        data_version = str(payload.get("data_version") or "")
        if not run_id or not snapshot_id or not data_version:
            raise ValueError("研究结果缺少 run_id/snapshot_id/data_version")
        signals = payload.get("signals", [])
        targets = payload.get("target_positions", [])
        if not isinstance(signals, list) or not isinstance(targets, list):
            raise ValueError("研究结果 signals/target_positions 必须是列表")
        self.bootstrap()
        with psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO quant_research_run
                    (run_id, run_type, snapshot_id, data_version, payload, created_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    run_id,
                    run_type,
                    snapshot_id,
                    data_version,
                    json.dumps(payload, ensure_ascii=False, default=_json_default),
                    datetime.now(UTC),
                ),
            )
            for item in signals:
                if not isinstance(item, Mapping):
                    raise ValueError("signal 条目必须是映射")
                cursor.execute(
                    """
                    INSERT INTO quant_signal_daily VALUES
                        (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (signal_id) DO NOTHING
                    """,
                    (
                        item["signal_id"],
                        run_id,
                        item["signal_date"],
                        item["execution_date"],
                        item["instrument_id"],
                        item["strategy_name"],
                        item["strategy_version"],
                        data_version,
                        json.dumps(item, ensure_ascii=False, default=_json_default),
                    ),
                )
            for item in targets:
                if not isinstance(item, Mapping):
                    raise ValueError("target_position 条目必须是映射")
                cursor.execute(
                    """
                    INSERT INTO quant_weekly_rotation_score VALUES
                        (%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT
                        (run_id, signal_date, instrument_id, strategy_name)
                    DO NOTHING
                    """,
                    (
                        run_id,
                        item["signal_date"],
                        item["instrument_id"],
                        item["strategy_name"],
                        item["score"],
                        item["target_weight"],
                        json.dumps(item, ensure_ascii=False, default=_json_default),
                    ),
                )
            connection.commit()

    def write_readiness(
        self,
        gate: str,
        trade_date: date,
        data_version: str,
        state: str,
        payload: Mapping[str, object],
    ) -> None:
        with psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO quant_data_readiness
                    (gate, trade_date, data_version, state, payload, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (gate, trade_date, data_version) DO UPDATE SET
                    state=excluded.state,
                    payload=excluded.payload,
                    evaluated_at=excluded.evaluated_at
                """,
                (
                    gate,
                    trade_date,
                    data_version,
                    state,
                    json.dumps(payload, ensure_ascii=False, default=_json_default),
                    datetime.now(UTC),
                ),
            )
            connection.commit()


def manifest_sha256(path: Path) -> str:
    """快照模块与存储审计共享的流式哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

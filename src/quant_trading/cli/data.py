"""数据管道 CLI：写入命令显式区分正式数据与离线 fixture。"""

from __future__ import annotations

import csv
import hashlib
from datetime import date
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer

from quant_trading.config import AppSettings
from quant_trading.data.calendar import latest_official_trade_day
from quant_trading.data.pipeline import PipelineOrchestrator, bootstrap_data_root
from quant_trading.data.snapshot import (
    SnapshotProfile,
    create_snapshot,
    pull_snapshot,
    restore_snapshot,
    verify_data_root,
    verify_snapshot,
)
from quant_trading.data.stock_pipeline import StockPipelineOrchestrator
from quant_trading.data.storage import ParquetDuckDBStore

data_app = typer.Typer(no_args_is_help=True, help="数据管道命令")
snapshot_app = typer.Typer(
    invoke_without_command=True,
    help="创建、拉取、校验或恢复快照",
)
data_app.add_typer(snapshot_app, name="snapshot")


def _parse_date(value: str) -> date:
    if value == "today":
        return date.today()
    return date.fromisoformat(value)


def _parse_instruments(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise typer.BadParameter("--instruments 不能为空")
    return items


def _settings(data_root: Path | None = None) -> AppSettings:
    settings = AppSettings()
    return (
        settings.model_copy(update={"data_root": data_root}) if data_root else settings
    )


def _pipeline(data_root: Path | None, offline_fixture: bool) -> PipelineOrchestrator:
    settings = _settings(data_root)
    return PipelineOrchestrator(settings=settings, offline_fixture=offline_fixture)


@data_app.command("bootstrap")
def data_bootstrap(
    data_root: Annotated[Path | None, typer.Option(help="覆盖数据根目录")] = None,
) -> None:
    """初始化目录和 DuckDB catalog；不会下载或伪造行情。"""

    root = data_root or AppSettings().data_root
    created = bootstrap_data_root(root)
    typer.echo(f"数据目录已就绪: {root}")
    for path in created:
        typer.echo(f"- {path}")


@data_app.command("refresh-master")
def data_refresh_master(
    as_of: Annotated[str, typer.Option("--as-of", help="YYYY-MM-DD / today")] = "today",
    data_root: Annotated[Path | None, typer.Option(help="数据根目录")] = None,
) -> None:
    """从正式来源刷新 point-in-time ETF 全市场清单。"""

    try:
        run = _pipeline(data_root, False).refresh_master(_parse_date(as_of))
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"refresh-master 完成: {run.run_id} records={run.published_count}")


@data_app.command("daily")
def data_daily(
    trade_date: Annotated[str, typer.Option("--trade-date", help="YYYY-MM-DD / today")],
    instruments: Annotated[
        str | None,
        typer.Option(help="仅覆盖本次任务，逗号分隔；不修改正式全市场配置"),
    ] = None,
    data_root: Annotated[Path | None, typer.Option(help="数据根目录")] = None,
    offline_fixture: Annotated[
        bool,
        typer.Option(help="使用两个明确标识的离线 fixture，仅供开发验收"),
    ] = False,
) -> None:
    """采集当日 raw/qfq，经双源门禁后发布 Gold。"""

    parsed = _parse_instruments(instruments)
    if offline_fixture and parsed is None:
        raise typer.BadParameter("--offline-fixture 必须同时指定 --instruments")
    try:
        run = _pipeline(data_root, offline_fixture).daily(
            _parse_date(trade_date), parsed
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"daily 完成: {run.run_id}\n"
        f"  data_version: {run.data_version}\n"
        f"  Gold: {run.published_count}, gate: {run.state}"
    )


@data_app.command("backfill")
def data_backfill(
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD / latest")],
    instruments: Annotated[
        str | None, typer.Option(help="逗号分隔的单次补数范围")
    ] = None,
    data_root: Annotated[Path | None, typer.Option(help="数据根目录")] = None,
    offline_fixture: Annotated[bool, typer.Option(help="仅供离线开发验收")] = False,
) -> None:
    """回填历史 raw/qfq；默认范围来自目标日期的 ETF master。"""

    start_date = _parse_date(start)
    end_date = (
        latest_official_trade_day(date.today()) if end == "latest" else _parse_date(end)
    )
    parsed = _parse_instruments(instruments)
    if offline_fixture and parsed is None:
        raise typer.BadParameter("--offline-fixture 必须同时指定 --instruments")
    try:
        run = _pipeline(data_root, offline_fixture).backfill(
            start_date, end_date, parsed
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"backfill 完成: {run.run_id}\n"
        f"  data_version: {run.data_version}\n"
        f"  Gold: {run.published_count}"
    )


@data_app.command("stock-backfill")
def stock_backfill(
    instruments: Annotated[str, typer.Option(help="逗号分隔的股票代码")],
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD / latest")],
    data_root: Annotated[Path | None, typer.Option(help="覆盖数据根目录")] = None,
) -> None:
    """使用 Tushare 交易约束与多源价格回填主题股票。"""
    parsed = _parse_instruments(instruments)
    assert parsed is not None
    end_date = (
        latest_official_trade_day(date.today()) if end == "latest" else _parse_date(end)
    )
    try:
        settings = _settings(data_root)
        run = StockPipelineOrchestrator(settings=settings).run(
            parsed, _parse_date(start), end_date
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"stock-backfill 完成: {run.run_id}\n"
        f"  data_version: {run.data_version}\n"
        f"  Gold: {run.published_count}, gate: {run.state}"
    )


@data_app.command("sector-import")
def sector_import(
    input_path: Annotated[Path, typer.Option("--input", help="板块成分 CSV 文件")],
    snapshot_date_text: Annotated[
        str, typer.Option("--snapshot-date", help="实际采集/发布日 YYYY-MM-DD")
    ],
    source_id: Annotated[
        str, typer.Option(help="来源标识，例如 akshare_eastmoney_concept")
    ],
    data_root: Annotated[Path | None, typer.Option(help="覆盖数据根目录")] = None,
) -> None:
    """导入 point-in-time 板块成分快照，CSV 需含 board_type/board_name/ts_code。"""
    if not input_path.is_file():
        raise typer.BadParameter(f"文件不存在: {input_path}")
    snapshot_date = _parse_date(snapshot_date_text)
    if snapshot_date > date.today():
        raise typer.BadParameter("snapshot-date 不能晚于今天")
    source = source_id.strip()
    if not source:
        raise typer.BadParameter("source-id 不能为空")
    content = input_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    try:
        with input_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"board_type", "board_name", "ts_code"}
            if not required.issubset(set(reader.fieldnames or ())):
                raise ValueError("CSV 必须包含 board_type,board_name,ts_code")
            rows = [
                {
                    "snapshot_date": snapshot_date,
                    "board_type": row["board_type"],
                    "board_name": row["board_name"],
                    "instrument_id": row["ts_code"],
                    "source_id": source,
                    "source_file_sha256": digest,
                }
                for row in reader
            ]
        if not rows:
            raise ValueError("板块成分 CSV 为空")
        settings = _settings(data_root)
        store = ParquetDuckDBStore(settings.data_root, min_free_gb=settings.min_free_gb)
        store.bootstrap()
        data_version = f"sector-{snapshot_date:%Y%m%d}-{digest[:12]}"
        run_id = f"sector-import-{snapshot_date:%Y%m%d}-{uuid4().hex[:8]}"
        with store.writer_lock():
            store.start_run(run_id, "sector-import", snapshot_date, data_version)
            existing_version = data_version in store.available_versions()
            if not existing_version:
                store.begin_data_version(
                    data_version,
                    parent_data_version=store.latest_ready_data_version(),
                    command="sector-import",
                )
            try:
                published = store.append_sector_constituents(rows, data_version)
                if not existing_version:
                    store.finish_data_version(data_version, "READY")
                store.finish_run(
                    run_id, "SUCCESS", f"rows={published}; sha256={digest}"
                )
            except Exception as exc:
                if not existing_version:
                    store.finish_data_version(data_version, "FAILED")
                store.finish_run(run_id, "FAILED", str(exc))
                raise
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"sector-import 完成: {data_version}\n"
        f"  rows: {published}, source_file_sha256: {digest}"
    )


@data_app.command("reconcile")
def data_reconcile(
    trade_date: Annotated[str, typer.Option("--trade-date", help="YYYY-MM-DD")],
    data_root: Annotated[Path | None, typer.Option(help="数据根目录")] = None,
) -> None:
    """从已持久化 Bronze 重算 Silver/Gold，不重新请求上游。"""

    try:
        run = _pipeline(data_root, False).reconcile(_parse_date(trade_date))
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"reconcile 完成: {run.run_id} Gold={run.published_count}")


@data_app.command("publish")
def data_publish(
    trade_date: Annotated[str, typer.Option("--trade-date", help="YYYY-MM-DD")],
    data_version: Annotated[str | None, typer.Option(help="锁定数据版本")] = None,
    data_root: Annotated[Path | None, typer.Option(help="数据根目录")] = None,
) -> None:
    """校验既有 DAILY_MARKET_READY；不会隐式采集或降低门禁。"""

    try:
        run = _pipeline(data_root, False).publish(_parse_date(trade_date), data_version)
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"publish 完成: {run.run_id} {run.data_version} {run.state}")


@data_app.command("compact")
def data_compact(
    data_root: Annotated[Path | None, typer.Option(help="数据根目录")] = None,
) -> None:
    """重写 Parquet 分区并刷新 DuckDB 视图。"""

    count = _pipeline(data_root, False).compact()
    typer.echo(f"compact 完成: {count} 个分区")


@data_app.command("status")
def data_status(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 10,
    data_root: Annotated[Path | None, typer.Option(help="数据根目录")] = None,
) -> None:
    """读取 DuckDB 中持久化的 ETL 运行记录。"""

    runs = ParquetDuckDBStore(data_root or AppSettings().data_root).status(limit)
    if not runs:
        typer.echo("暂无 ETL 运行记录")
        return
    for run in runs:
        typer.echo(
            f"{run['status']:8s} {run['command']:14s} {run['trade_date']:10s} "
            f"{run['data_version']} {run['detail']}"
        )


@snapshot_app.callback()
def snapshot_create_default(
    ctx: typer.Context,
    profile: Annotated[SnapshotProfile, typer.Option(help="dev / full")] = "dev",
    data_version: Annotated[str | None, typer.Option(help="锁定 Gold 版本")] = None,
    retain: Annotated[int, typer.Option(min=1)] = 7,
    data_root: Annotated[Path | None, typer.Option(help="数据根目录")] = None,
    snapshot_root: Annotated[Path | None, typer.Option(help="快照根目录")] = None,
) -> None:
    """不带子命令时创建快照，兼容 ``quant data snapshot --profile dev``。"""

    if ctx.invoked_subcommand is not None:
        return
    settings = AppSettings()
    root = data_root or settings.data_root
    store = ParquetDuckDBStore(root)
    version = data_version or (
        store.available_versions()[-1] if store.available_versions() else ""
    )
    if not version:
        raise typer.BadParameter("没有可快照的 Gold data_version")
    try:
        manifest = create_snapshot(
            root,
            snapshot_root or settings.snapshot_root,
            version,
            profile=profile,
            retain=retain,
        )
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"snapshot 创建完成: {manifest.snapshot_id} "
        f"profile={manifest.profile} files={len(manifest.files)}"
    )


@snapshot_app.command("pull")
def snapshot_pull(
    remote: Annotated[str, typer.Option(help="SSH 别名或 user@host")] = "aliyun",
    profile: Annotated[SnapshotProfile, typer.Option(help="dev / full")] = "dev",
    snapshot: Annotated[str | None, typer.Option(help="锁定 snapshot_id")] = None,
    backup_existing: Annotated[
        bool, typer.Option(help="备份后替换未知本地目录")
    ] = False,
) -> None:
    settings = AppSettings()
    try:
        manifest = pull_snapshot(
            remote=remote,
            remote_snapshot_root=settings.remote_snapshot_root,
            local_snapshot_root=settings.snapshot_root,
            data_root=settings.data_root,
            profile=profile,
            snapshot_id=snapshot,
            backup_existing=backup_existing,
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"snapshot 拉取并恢复完成: {manifest.snapshot_id}")


@snapshot_app.command("verify")
def snapshot_verify(
    snapshot_dir: Annotated[Path | None, typer.Option(help="指定快照目录")] = None,
    data_root: Annotated[Path | None, typer.Option(help="验证已恢复的数据目录")] = None,
) -> None:
    settings = AppSettings()
    try:
        result = (
            verify_snapshot(snapshot_dir)
            if snapshot_dir is not None
            else verify_data_root(data_root or settings.data_root)
        )
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"snapshot 校验通过: {result}")


@snapshot_app.command("restore")
def snapshot_restore(
    snapshot_dir: Annotated[Path, typer.Argument(help="本地快照目录")],
    data_root: Annotated[Path | None, typer.Option(help="目标数据目录")] = None,
    backup_existing: Annotated[
        bool, typer.Option(help="备份后替换未知本地目录")
    ] = False,
) -> None:
    try:
        manifest = restore_snapshot(
            snapshot_dir,
            data_root or AppSettings().data_root,
            backup_existing=backup_existing,
        )
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"snapshot 恢复完成: {manifest.snapshot_id}")

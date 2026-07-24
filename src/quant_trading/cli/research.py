"""策略运行与因子检查命令。

正式研究只读取已经恢复并验证过的 Gold 快照；本模块不采集、不融合、也不写行情。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from quant_trading.config import (
    AppSettings,
    BacktestConfig,
    ConfigError,
    StrategyFile,
    UniverseConfig,
    load_backtest_config,
    load_strategy_file,
    load_universe_config,
)
from quant_trading.data.pipeline import PipelineOrchestrator
from quant_trading.data.readiness import (
    ReadinessError,
    ReadinessGate,
    ReadinessRegistry,
    ReadinessStatus,
)
from quant_trading.data.snapshot import verify_data_root
from quant_trading.data.storage import ParquetDuckDBStore, PostgresAuditStore
from quant_trading.models import BarAdjustment, MarketBar, Signal, TargetPosition
from quant_trading.research.backtest import run_backtest
from quant_trading.research.factors import (
    ma_distance,
    max_drawdown,
    period_return,
    realized_volatility,
)
from quant_trading.research.portfolio import AllocationConstraints, allocate
from quant_trading.research.strategy import (
    Strategy,
    StrategyContext,
    StrategyRunner,
    targets_to_signals,
)
from quant_trading.research.strategy_registry import (
    StrategyRegistryError,
    create_strategy,
    list_strategy_names,
    load_strategy_configs,
)
from quant_trading.research.universe import UniverseMode, UniverseResolver

research_app = typer.Typer(no_args_is_help=True, help="策略、因子与回放命令")
strategies_app = typer.Typer(no_args_is_help=True, help="策略注册与配置命令")
research_app.add_typer(strategies_app, name="strategies")


@strategies_app.command("list")
def strategies_list() -> None:
    """列出代码中显式注册的全部策略。"""
    for name in list_strategy_names():
        typer.echo(name)


@strategies_app.command("validate")
def strategies_validate(
    config_dir: Annotated[Path, typer.Option(help="策略配置目录")] = Path(
        "configs/strategies"
    ),
) -> None:
    """校验注册表、配置名称、门禁名称和具体策略参数。"""
    if not config_dir.is_dir():
        raise typer.BadParameter(f"策略配置目录不存在: {config_dir}")

    errors: list[str] = []
    total_weight = 0.0
    files = sorted(config_dir.glob("*.yaml"))
    for path in files:
        try:
            config = load_strategy_file(path)
            if path.stem != config.name:
                raise ConfigError(
                    f"文件名 {path.stem!r} 与策略名 {config.name!r} 不一致"
                )
            unknown_gates = set(config.required_readiness) - {
                gate.value for gate in ReadinessGate
            }
            if unknown_gates:
                names = ", ".join(sorted(unknown_gates))
                raise ConfigError(f"存在未知门禁: {names}")
            create_strategy(config)
            if config.enabled:
                total_weight += config.capital_weight
            typer.echo(f"OK  {config.name} {config.version}")
        except (ConfigError, StrategyRegistryError, ValueError) as exc:
            errors.append(f"{path}: {exc}")

    if not files:
        errors.append(f"{config_dir}: 没有找到策略 YAML")
    if total_weight > 1.0:
        errors.append(f"capital_weight 合计 {total_weight:.4f} 超过 1.0")
    if errors:
        for msg in errors:
            typer.echo(f"ERROR  {msg}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"全部通过，共 {len(files)} 个策略配置")


def _open_snapshot_store() -> tuple[ParquetDuckDBStore, str]:
    """验证快照来源并以只读模式打开本地 Gold 数据。"""
    settings = AppSettings()
    evidence = verify_data_root(settings.data_root)
    snapshot_id = str(evidence.get("snapshot_id") or "")
    if not snapshot_id:
        raise RuntimeError("快照来源缺少 snapshot_id")
    return ParquetDuckDBStore(settings.data_root, read_only=True), snapshot_id


def _config_hash(config_path: Path, universe_path: Path) -> str:
    digest = hashlib.sha256()
    for path in (config_path, universe_path):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _persist_research_result(payload: Mapping[str, object], run_type: str) -> None:
    settings = AppSettings()
    if not settings.postgres_dsn:
        raise ConfigError("--persist-postgres 需要 QUANT_POSTGRES_DSN")
    PostgresAuditStore(settings.postgres_dsn).write_research_result(
        payload, run_type=run_type
    )


def _universe_key(strategy_name: str) -> str:
    return {
        "cross_sectional": "etf_cross_sectional",
    }.get(strategy_name, strategy_name)


def _resolve_universe(
    strategy_name: str,
    universe_config: UniverseConfig,
    store: ParquetDuckDBStore,
    trade_date: date,
    data_version: str,
) -> tuple[str, ...]:
    key = _universe_key(strategy_name)
    try:
        entry = universe_config.universes[key]
    except KeyError as exc:
        raise ConfigError(f"策略 {strategy_name} 缺少 universes.{key} 配置") from exc
    if entry.mode == "configured":
        instruments = tuple(sorted(set(entry.instruments)))
        return instruments
    if entry.mode == "theme_snapshot":
        members = store.get_theme_members(
            trade_date,
            data_version=data_version,
            board_types=entry.board_types,
            themes=entry.themes,
        )
        return tuple(sorted({item for values in members.values() for item in values}))
    selection = UniverseResolver(store).resolve(
        trade_date,
        data_version=data_version,
        mode=UniverseMode.POINT_IN_TIME,
    )
    return selection.instruments


def _risk_state(benchmark_bars: Sequence[MarketBar]) -> str:
    """使用截至信号日的 20 日均线生成透明风险开关。"""
    ordered = sorted(benchmark_bars, key=lambda bar: bar.trade_date)
    if len(ordered) < 20:
        raise RuntimeError("基准历史不足 20 日，无法计算风险状态")
    closes = [bar.close for bar in ordered[-20:]]
    return "risk_on" if closes[-1] >= sum(closes) / len(closes) else "risk_off"


def _build_context(
    *,
    strategy: Strategy,
    config_path: Path,
    universe_path: Path,
    universe_config: UniverseConfig,
    store: ParquetDuckDBStore,
    snapshot_id: str,
    trade_date: date,
    data_version: str,
    readiness_override: Mapping[str, ReadinessStatus] | None = None,
) -> StrategyContext:
    universe = _resolve_universe(
        strategy.name, universe_config, store, trade_date, data_version
    )
    entry = universe_config.universes[_universe_key(strategy.name)]
    theme_members = (
        store.get_theme_members(
            trade_date,
            data_version=data_version,
            board_types=entry.board_types,
            themes=entry.themes,
        )
        if entry.mode == "theme_snapshot"
        else {}
    )
    start = trade_date - timedelta(days=550)
    if strategy.asset_type == "stock":
        bars = store.get_stock_bars(
            universe,
            start,
            trade_date,
            adjustment=BarAdjustment.QFQ,
            data_version=data_version,
        )
    else:
        bars = store.get_bars(
            universe,
            start,
            trade_date,
            adjustment=BarAdjustment.QFQ,
            data_version=data_version,
        )
    benchmark_id = universe_config.benchmarks.get("default")
    if not benchmark_id:
        raise ConfigError("universes.benchmarks.default 未配置")
    # 基准可能是指数(000*.SH)或ETF(51*.SH)，按前缀路由到对应存储
    if benchmark_id.startswith("000") or benchmark_id.startswith("399"):
        benchmark = store.get_index_bars(
            benchmark_id, start, trade_date, data_version=data_version
        )
    else:
        benchmark = store.get_bars(
            (benchmark_id,),
            start,
            trade_date,
            adjustment=BarAdjustment.QFQ,
            data_version=data_version,
        )
    calendar = store.get_trade_calendar(start, trade_date, data_version=data_version)
    if readiness_override is None:
        readiness = {
            gate: ReadinessRegistry(store).get(
                ReadinessGate(gate), trade_date, data_version
            )
            for gate in strategy.required_readiness
        }
    else:
        readiness = {
            gate: readiness_override[gate] for gate in strategy.required_readiness
        }
    return StrategyContext(
        trade_date=trade_date,
        data_version=data_version,
        snapshot_id=snapshot_id,
        bars=tuple(bars),
        benchmark_bars=tuple(benchmark),
        calendar=calendar,
        universe=universe,
        theme_members=theme_members,
        readiness=readiness,
        risk_state=_risk_state(benchmark),
        config_hash=_config_hash(config_path, universe_path),
    )


def _selected_configs(
    config_dir: Path, strategy_name: str | None
) -> dict[str, StrategyFile]:
    configs = load_strategy_configs(config_dir)
    if strategy_name is not None:
        config = configs.get(strategy_name)
        if config is None:
            raise ConfigError(f"找不到策略配置: {strategy_name}")
        return {strategy_name: config}
    return {name: config for name, config in configs.items() if config.enabled}


def _execute_research_run(
    *,
    trade_date: date,
    data_version: str,
    strategy_name: str | None,
    config_dir: Path,
    universe_path: Path,
) -> dict[str, object]:
    store, snapshot_id = _open_snapshot_store()
    universe_config = load_universe_config(universe_path)
    configs = _selected_configs(config_dir, strategy_name)
    if not configs:
        raise ConfigError("没有启用的策略")
    strategies = {name: create_strategy(config) for name, config in configs.items()}
    runner = StrategyRunner(strategies, ReadinessRegistry(store))
    contexts = {
        name: _build_context(
            strategy=strategy,
            config_path=config_dir / f"{name}.yaml",
            universe_path=universe_path,
            universe_config=universe_config,
            store=store,
            snapshot_id=snapshot_id,
            trade_date=trade_date,
            data_version=data_version,
        )
        for name, strategy in strategies.items()
    }
    all_targets = runner.run_all(contexts)
    next_trade_day = store.get_next_trade_day(trade_date, data_version=data_version)
    if next_trade_day is None:
        raise RuntimeError("交易日历中没有 T+1 执行日")
    signals = [
        signal
        for targets in all_targets.values()
        for signal in targets_to_signals(targets, execution_date=next_trade_day)
    ]
    weights = {name: configs[name].capital_weight for name in strategies}
    risk_states = {context.risk_state for context in contexts.values()}
    if len(risk_states) != 1:
        raise RuntimeError("多策略运行的 risk_state 不一致")
    allocation = allocate(
        all_targets,
        weights,
        AllocationConstraints(),
        risk_state=next(iter(risk_states)),
    )
    run_identity = "|".join(
        (snapshot_id, data_version, trade_date.isoformat(), *sorted(strategies))
    )
    return {
        "run_id": "RESEARCH-" + hashlib.sha256(run_identity.encode()).hexdigest()[:16],
        "created_at": datetime.now(UTC).isoformat(),
        "git_commit": _git_commit(),
        "schema_version": "1.0.0",
        "trade_date": trade_date.isoformat(),
        "execution_date": next_trade_day.isoformat(),
        "snapshot_id": snapshot_id,
        "data_version": data_version,
        "strategies": {name: strategy.version for name, strategy in strategies.items()},
        "config_hashes": {
            name: contexts[name].config_hash for name in sorted(contexts)
        },
        "readiness": {
            name: {
                gate: status.state.value
                for gate, status in contexts[name].readiness.items()
            }
            for name in sorted(contexts)
        },
        "signals": [signal.model_dump(mode="json") for signal in signals],
        "target_positions": [
            target.model_dump(mode="json")
            for targets in all_targets.values()
            for target in targets
        ],
        "positions": allocation.positions,
        "adjustments": allocation.adjustments,
    }


@research_app.command("run")
def research_run(
    trade_date_text: Annotated[str, typer.Option("--trade-date", help="YYYY-MM-DD")],
    data_version: Annotated[str, typer.Option(help="锁定的 Gold 数据版本")],
    strategy_name: Annotated[
        str | None, typer.Option("--strategy", help="只运行一个已注册策略")
    ] = None,
    config_dir: Annotated[Path, typer.Option(help="策略配置目录")] = Path(
        "configs/strategies"
    ),
    universe_path: Annotated[Path, typer.Option(help="资产池配置文件")] = Path(
        "configs/universes.yaml"
    ),
    output: Annotated[Path | None, typer.Option(help="同时归档为 JSON 文件")] = None,
    persist_postgres: Annotated[
        bool, typer.Option(help="显式写入远程 PostgreSQL 研究结果")
    ] = False,
) -> None:
    """从已验证快照运行策略，门禁不通过时明确失败。"""
    try:
        payload = _execute_research_run(
            trade_date=date.fromisoformat(trade_date_text),
            data_version=data_version,
            strategy_name=strategy_name,
            config_dir=config_dir,
            universe_path=universe_path,
        )
        if persist_postgres:
            _persist_research_result(payload, "research")
    except (ConfigError, StrategyRegistryError, RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    typer.echo(rendered)


def _read_run_artifact(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"研究运行文件不存在: {path}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("研究运行文件根节点必须是 JSON object")
    return loaded


def _rebalance_dates(
    calendar: Sequence[date], start: date, end: date, frequency: str
) -> tuple[date, ...]:
    """从锁定交易日历生成确定的日频或周频信号日。"""
    selected = sorted(day for day in calendar if start <= day <= end)
    if frequency == "daily":
        return tuple(selected)
    if frequency != "weekly":
        raise ValueError(f"不支持的策略频率: {frequency}")
    by_week: dict[tuple[int, int], date] = {}
    for day in selected:
        iso = day.isocalendar()
        by_week[(iso.year, iso.week)] = day
    return tuple(by_week[key] for key in sorted(by_week))


def _historical_backtest_payload(
    *,
    strategy_name: str,
    start: date,
    end: date,
    data_version: str,
    config_dir: Path,
    universe_path: Path,
    backtest_config: Path,
) -> dict[str, object]:
    """在同一快照上逐期生成目标并撮合，门禁失败期只记录、不降级。"""
    if start >= end:
        raise ValueError("历史回测要求 start 早于 end")
    store, snapshot_id = _open_snapshot_store()
    strategy_path = config_dir / f"{strategy_name}.yaml"
    strategy_config = load_strategy_file(strategy_path)
    strategy = create_strategy(strategy_config)
    universe_config = load_universe_config(universe_path)
    calendar = store.get_trade_calendar(start, end, data_version=data_version)
    signal_dates = _rebalance_dates(calendar, start, end, strategy.frequency)
    if not signal_dates:
        raise RuntimeError("回测区间没有交易日")

    evaluator = PipelineOrchestrator(
        settings=AppSettings(),
        store=store,
        providers=(),
        master_providers=(),
    )
    runner = StrategyRunner({strategy.name: strategy}, ReadinessRegistry(store))
    signals: list[Signal] = []
    targets: list[TargetPosition] = []
    previous_instruments: set[str] = set()
    readiness_audit: dict[str, dict[str, str]] = {}
    config_hash = _config_hash(strategy_path, universe_path)

    for signal_date in signal_dates:
        next_trade_day = store.get_next_trade_day(
            signal_date, data_version=data_version
        )
        if next_trade_day is None or next_trade_day > end:
            continue
        statuses = evaluator.evaluate_research_readiness(
            signal_date, data_version, persist=False
        )
        by_gate = {status.gate.value: status for status in statuses}
        readiness_audit[signal_date.isoformat()] = {
            gate: by_gate[gate].state.value for gate in strategy.required_readiness
        }
        try:
            context = _build_context(
                strategy=strategy,
                config_path=strategy_path,
                universe_path=universe_path,
                universe_config=universe_config,
                store=store,
                snapshot_id=snapshot_id,
                trade_date=signal_date,
                data_version=data_version,
                readiness_override=by_gate,
            )
            raw_targets = runner.run_single(strategy, context)
        except (ReadinessError, RuntimeError) as exc:
            readiness_audit[signal_date.isoformat()] = {
                **readiness_audit[signal_date.isoformat()],
                "decision": "SKIPPED",
                "reason": str(exc),
            }
            continue

        allocation = allocate(
            {strategy.name: raw_targets},
            {strategy.name: strategy_config.capital_weight},
            AllocationConstraints(),
            risk_state=context.risk_state,
        )
        raw_by_instrument = {item.instrument_id: item for item in raw_targets}
        current_instruments = set(allocation.positions)
        period_targets: list[TargetPosition] = []
        for instrument, weight in sorted(allocation.positions.items()):
            source = raw_by_instrument[instrument]
            period_targets.append(source.model_copy(update={"target_weight": weight}))
        for instrument in sorted(previous_instruments - current_instruments):
            period_targets.append(
                TargetPosition(
                    instrument_id=instrument,
                    asset_type="stock" if strategy.asset_type == "stock" else "etf",
                    signal_date=signal_date,
                    strategy_name=strategy.name,
                    strategy_version=strategy.version,
                    data_version=data_version,
                    config_hash=config_hash,
                    target_weight=0.0,
                    score=0.0,
                    reason="本期未入选，目标仓位归零",
                )
            )
        targets.extend(period_targets)
        signals.extend(
            targets_to_signals(period_targets, execution_date=next_trade_day)
        )
        previous_instruments = current_instruments

    run_key = "|".join(
        (
            snapshot_id,
            data_version,
            strategy.name,
            start.isoformat(),
            end.isoformat(),
            config_hash,
        )
    )
    run_id = "BACKTEST-" + hashlib.sha256(run_key.encode()).hexdigest()[:16]
    common: dict[str, object] = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "git_commit": _git_commit(),
        "schema_version": "1.0.0",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "benchmark": universe_config.benchmarks.get("default", ""),
        "snapshot_id": snapshot_id,
        "data_version": data_version,
        "strategies": {strategy.name: strategy.version},
        "config_hashes": {strategy.name: config_hash},
        "readiness": readiness_audit,
        "backtest_config": load_backtest_config(backtest_config).model_dump(
            mode="json"
        ),
        "signals": [item.model_dump(mode="json") for item in signals],
        "target_positions": [item.model_dump(mode="json") for item in targets],
    }
    if not signals:
        return {
            **common,
            "status": "NO_SIGNALS",
            "metrics": {"total_return": 0.0, "total_trades": 0.0},
            "trades": [],
            "executions": [],
            "snapshots": [],
        }
    instruments = tuple(sorted({signal.instrument_id for signal in signals}))
    bar_reader = (
        store.get_stock_bars if strategy.asset_type == "stock" else store.get_bars
    )
    bars = list(
        bar_reader(
            instruments,
            start,
            end,
            adjustment=BarAdjustment.RAW,
            data_version=data_version,
        )
    )
    result = run_backtest(
        signals,
        targets,
        bars,
        load_backtest_config(backtest_config),
        data_version=data_version,
    )
    return {
        **common,
        "status": "SUCCESS",
        "metrics": result.metrics,
        "trades": [item.model_dump(mode="json") for item in result.trades],
        "executions": [item.model_dump(mode="json") for item in result.executions],
        "snapshots": [item.model_dump(mode="json") for item in result.snapshots],
    }


@research_app.command("backtest")
def research_backtest(
    run_file: Annotated[
        Path | None, typer.Option("--run-file", help="撮合已有 research run JSON")
    ] = None,
    strategy_name: Annotated[
        str | None, typer.Option("--strategy", help="逐期运行指定策略")
    ] = None,
    start_text: Annotated[
        str | None, typer.Option("--start", help="历史回测开始日 YYYY-MM-DD")
    ] = None,
    end_text: Annotated[
        str | None, typer.Option("--end", help="回测结束日 YYYY-MM-DD")
    ] = None,
    data_version_option: Annotated[
        str | None, typer.Option("--data-version", help="锁定 Gold 逻辑版本")
    ] = None,
    backtest_config: Annotated[Path, typer.Option(help="回测配置文件")] = Path(
        "configs/backtest.yaml"
    ),
    config_dir: Annotated[Path, typer.Option(help="策略配置目录")] = Path(
        "configs/strategies"
    ),
    universe_path: Annotated[Path, typer.Option(help="资产池配置文件")] = Path(
        "configs/universes.yaml"
    ),
    output_path: Annotated[
        Path | None, typer.Option("--output", help="归档回测 JSON")
    ] = None,
    persist_postgres: Annotated[
        bool, typer.Option(help="显式写入远程 PostgreSQL 研究结果")
    ] = False,
) -> None:
    """撮合归档信号，或在锁定快照上执行多期历史策略回测。"""
    try:
        if run_file is None:
            if not all((strategy_name, start_text, end_text, data_version_option)):
                raise ValueError(
                    "历史模式必须同时提供 --strategy/--start/--end/--data-version"
                )
            output = _historical_backtest_payload(
                strategy_name=str(strategy_name),
                start=date.fromisoformat(str(start_text)),
                end=date.fromisoformat(str(end_text)),
                data_version=str(data_version_option),
                config_dir=config_dir,
                universe_path=universe_path,
                backtest_config=backtest_config,
            )
            if persist_postgres:
                _persist_research_result(output, "backtest")
            rendered = json.dumps(output, ensure_ascii=False, indent=2)
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(rendered + "\n", encoding="utf-8")
            typer.echo(rendered)
            return
        if end_text is None:
            raise ValueError("--run-file 模式必须提供 --end")
        payload = _read_run_artifact(run_file)
        data_version = str(payload["data_version"])
        snapshot_id = str(payload["snapshot_id"])
        signals_raw = payload.get("signals")
        targets_raw = payload.get("target_positions")
        if not isinstance(signals_raw, list) or not isinstance(targets_raw, list):
            raise ValueError("运行文件缺少 signals 或 target_positions")
        signals = [Signal.model_validate(item) for item in signals_raw]
        targets = [TargetPosition.model_validate(item) for item in targets_raw]
        store, current_snapshot = _open_snapshot_store()
        if current_snapshot != snapshot_id:
            raise ValueError(
                f"快照不一致: run={snapshot_id}, current={current_snapshot}"
            )
        end = date.fromisoformat(end_text)
        if not signals:
            output = {
                "status": "NO_SIGNALS",
                "run_id": "BACKTEST-" + str(payload.get("run_id") or "unknown"),
                "source_run_id": payload.get("run_id"),
                "snapshot_id": snapshot_id,
                "data_version": data_version,
                "strategies": payload.get("strategies", {}),
                "config_hashes": payload.get("config_hashes", {}),
                "readiness": payload.get("readiness", {}),
                "metrics": {
                    "total_return": 0.0,
                    "total_trades": 0.0,
                },
                "trades": [],
                "executions": [],
                "snapshots": [],
            }
            rendered = json.dumps(output, ensure_ascii=False, indent=2)
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(rendered + "\n", encoding="utf-8")
            typer.echo(rendered)
            return
        start = min(signal.signal_date for signal in signals)
        asset_types = {signal.asset_type for signal in signals}
        bars: list[MarketBar] = []
        for asset_type in sorted(asset_types):
            scoped = tuple(
                sorted(
                    {
                        signal.instrument_id
                        for signal in signals
                        if signal.asset_type == asset_type
                    }
                )
            )
            bar_reader = (
                store.get_stock_bars if asset_type == "stock" else store.get_bars
            )
            bars.extend(
                bar_reader(
                    scoped,
                    start,
                    end,
                    adjustment=BarAdjustment.RAW,
                    data_version=data_version,
                )
            )
        config: BacktestConfig = load_backtest_config(backtest_config)
        result = run_backtest(
            signals,
            targets,
            bars,
            config,
            data_version=data_version,
        )
    except (KeyError, TypeError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    output = {
        "run_id": "BACKTEST-" + str(payload.get("run_id") or "unknown"),
        "source_run_id": payload.get("run_id"),
        "snapshot_id": snapshot_id,
        "data_version": data_version,
        "strategies": payload.get("strategies", {}),
        "config_hashes": payload.get("config_hashes", {}),
        "readiness": payload.get("readiness", {}),
        "metrics": result.metrics,
        "trades": [item.model_dump(mode="json") for item in result.trades],
        "executions": [item.model_dump(mode="json") for item in result.executions],
        "snapshots": [item.model_dump(mode="json") for item in result.snapshots],
    }
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    typer.echo(rendered)


@research_app.command("replay")
def research_replay(
    run_file: Annotated[Path, typer.Option("--run-file", help="research run JSON")],
    config_dir: Annotated[Path, typer.Option(help="策略配置目录")] = Path(
        "configs/strategies"
    ),
    universe_path: Annotated[Path, typer.Option(help="资产池配置文件")] = Path(
        "configs/universes.yaml"
    ),
) -> None:
    """在相同快照和配置上重放策略，并比较稳定输出。"""
    try:
        expected = _read_run_artifact(run_file)
        strategies = expected.get("strategies")
        if not isinstance(strategies, dict) or not strategies:
            raise ValueError("运行文件缺少 strategies")
        strategy_name = str(next(iter(strategies))) if len(strategies) == 1 else None
        actual = _execute_research_run(
            trade_date=date.fromisoformat(str(expected["trade_date"])),
            data_version=str(expected["data_version"]),
            strategy_name=strategy_name,
            config_dir=config_dir,
            universe_path=universe_path,
        )
        if actual.get("strategies") != strategies:
            raise RuntimeError("重放策略集合或版本不一致")
        for key in (
            "snapshot_id",
            "data_version",
            "signals",
            "target_positions",
            "positions",
        ):
            if actual.get(key) != expected.get(key):
                raise RuntimeError(f"重放不一致: {key}")
    except (
        KeyError,
        TypeError,
        json.JSONDecodeError,
        ConfigError,
        StrategyRegistryError,
        RuntimeError,
        ValueError,
    ) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "status": "MATCH",
                "snapshot_id": actual["snapshot_id"],
                "data_version": actual["data_version"],
                "strategies": actual["strategies"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@research_app.command("build-factors")
def build_factors(
    trade_date_text: Annotated[str, typer.Option("--trade-date", help="YYYY-MM-DD")],
    data_version: Annotated[str, typer.Option(help="锁定的 Gold 数据版本")],
    instruments: Annotated[
        str, typer.Option(help="逗号分隔 ETF 代码；不允许隐式默认候选池")
    ],
) -> None:
    """从锁定 qfq Gold 序列计算公共因子；历史不足输出 null。"""
    try:
        trade_date = date.fromisoformat(trade_date_text)
        store, snapshot_id = _open_snapshot_store()
        universe = tuple(
            sorted({item.strip() for item in instruments.split(",") if item.strip()})
        )
        if not universe:
            raise ValueError("instruments 不能为空")
        bars = store.get_bars(
            universe,
            trade_date - timedelta(days=180),
            trade_date,
            adjustment=BarAdjustment.QFQ,
            data_version=data_version,
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    by_instrument: dict[str, list[MarketBar]] = {}
    for bar in bars:
        by_instrument.setdefault(bar.instrument_id, []).append(bar)
    result: dict[str, object] = {
        "trade_date": trade_date.isoformat(),
        "snapshot_id": snapshot_id,
        "data_version": data_version,
        "adjustment": "qfq",
        "factors": {},
    }
    factor_result = result["factors"]
    assert isinstance(factor_result, dict)
    for instrument in universe:
        closes = [
            bar.close
            for bar in sorted(
                by_instrument.get(instrument, []), key=lambda item: item.trade_date
            )
        ]
        factor_result[instrument] = {
            "ret_20d": period_return(closes, 20),
            "volatility_20d": (
                realized_volatility(closes[-21:]) if len(closes) >= 21 else None
            ),
            "drawdown_60d": max_drawdown(closes[-60:]) if len(closes) >= 60 else None,
            "ma20_distance": ma_distance(closes[-20:]) if len(closes) >= 20 else None,
        }
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))

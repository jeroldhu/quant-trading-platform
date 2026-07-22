"""统一命令行入口与子命令装配。"""

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from quant_trading import __version__
from quant_trading.reporting.ai_evaluation import create_evaluator
from quant_trading.reporting.reports import ReportType, generate_report_bundle

from .data import data_app
from .research import research_app

app = typer.Typer(no_args_is_help=True, help="A 股量化研究平台")
app.add_typer(data_app, name="data")
app.add_typer(research_app, name="research")

# -- scheduler ----------------------------------------------------------


scheduler_app = typer.Typer(no_args_is_help=True, help="定时调度管理")
app.add_typer(scheduler_app, name="scheduler")


def _compose(args: list[str], *, capture: bool = True) -> None:
    compose_file = Path("docker-compose.yml")
    if not compose_file.is_file():
        raise typer.BadParameter("当前目录缺少 docker-compose.yml")
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), *args],
            check=False,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError as exc:
        typer.echo("ERROR: 未安装 docker 命令", err=True)
        raise typer.Exit(code=1) from exc
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if output:
        typer.echo(output)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)


@scheduler_app.command("status")
def scheduler_status() -> None:
    """查看调度状态。"""
    _compose(["--profile", "server", "ps", "scheduler"])


@scheduler_app.command("start")
def scheduler_start() -> None:
    """启动定时采集。"""
    _compose(["--profile", "server", "up", "-d", "--build", "scheduler"])


@scheduler_app.command("stop")
def scheduler_stop() -> None:
    """停止定时采集。"""
    _compose(["--profile", "server", "stop", "scheduler"])


@scheduler_app.command("logs")
def scheduler_logs(
    lines: Annotated[int, typer.Option("--lines", min=1, max=10_000)] = 100,
    follow: Annotated[bool, typer.Option("--follow", help="持续跟随日志")] = False,
) -> None:
    """查看 scheduler 容器日志。"""
    args = ["--profile", "server", "logs", "--tail", str(lines)]
    if follow:
        args.append("--follow")
    args.append("scheduler")
    _compose(args, capture=not follow)


# -- report -------------------------------------------------------------


report_app = typer.Typer(no_args_is_help=True, help="报告生成命令")
app.add_typer(report_app, name="report")


def _generate_report(report_type: str, input_path: Path, output_dir: Path) -> None:
    try:
        loaded = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("报告输入根节点必须是 JSON object")
        artifacts = generate_report_bundle(
            output_dir,
            cast(ReportType, report_type),
            loaded,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for artifact in artifacts:
        typer.echo(f"{artifact.path} sha256={artifact.content_sha256}")


@report_app.command("daily")
def report_daily(
    input_path: Annotated[Path, typer.Option("--input", help="research run JSON")],
    output_dir: Annotated[Path, typer.Option(help="报告输出目录")] = Path("reports"),
) -> None:
    """生成 JSON、Markdown、HTML 每日报告。"""
    _generate_report("daily", input_path, output_dir)


@report_app.command("weekly")
def report_weekly(
    input_path: Annotated[Path, typer.Option("--input", help="周度汇总 JSON")],
    output_dir: Annotated[Path, typer.Option(help="报告输出目录")] = Path("reports"),
) -> None:
    """生成 JSON、Markdown、HTML 周报。"""
    _generate_report("weekly", input_path, output_dir)


@report_app.command("backtest")
def report_backtest(
    input_path: Annotated[Path, typer.Option("--input", help="回测结果 JSON")],
    output_dir: Annotated[Path, typer.Option(help="报告输出目录")] = Path("reports"),
) -> None:
    """生成回测报告，有净值序列时附 SVG 图。"""
    _generate_report("backtest", input_path, output_dir)


@report_app.command("quality")
def report_quality(
    input_path: Annotated[Path, typer.Option("--input", help="质量汇总 JSON")],
    output_dir: Annotated[Path, typer.Option(help="报告输出目录")] = Path("reports"),
) -> None:
    """生成数据质量报告。"""
    _generate_report("quality", input_path, output_dir)


# -- ai -----------------------------------------------------------------


ai_app = typer.Typer(no_args_is_help=True, help="AI 评估命令")
app.add_typer(ai_app, name="ai")


@ai_app.command("status")
def ai_status() -> None:
    """查看本地 AI 配置状态，不发起计费请求。"""
    from quant_trading.config import load_reporting_config

    config = load_reporting_config().ai
    typer.echo(
        json.dumps(
            {
                "enabled": config.enabled,
                "api_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
                "model": config.model,
                "base_url": config.base_url,
                "prompt_version": config.prompt_version,
                "max_total_tokens_per_evaluation": (
                    config.max_total_tokens_per_evaluation
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@ai_app.command("evaluate")
def ai_evaluate(
    dimension: Annotated[
        str, typer.Option(help="backtest/signal/signal_explanation/anomaly")
    ],
    input_path: Annotated[Path, typer.Option("--input", help="报告或研究 JSON")],
    archive_dir: Annotated[Path, typer.Option(help="AI 独立归档目录")] = Path(
        "reports/ai-evaluations"
    ),
) -> None:
    """对白名单数据运行一次只读评估；失败不修改输入或正式结果。"""
    from quant_trading.config import load_reporting_config

    try:
        loaded = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("AI 输入根节点必须是 JSON object")
        evaluator = create_evaluator(
            load_reporting_config().ai, archive_dir=archive_dir
        )
        result = evaluator.explain(dimension, loaded)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


# -- config -------------------------------------------------------------


config_app = typer.Typer(no_args_is_help=True, help="配置管理命令")
app.add_typer(config_app, name="config")


@config_app.command("validate")
def config_validate() -> None:
    """校验全部配置文件。"""
    from quant_trading.config import (
        load_backtest_config,
        load_pipeline_config,
        load_reporting_config,
        load_trading_calendar_config,
        load_universe_config,
    )

    errors: list[str] = []
    for name, loader in [
        ("pipeline", load_pipeline_config),
        ("trading_calendar", load_trading_calendar_config),
        ("universes", load_universe_config),
        ("backtest", load_backtest_config),
        ("reporting", load_reporting_config),
    ]:
        try:
            loader()
            typer.echo(f"OK  {name}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        for msg in errors:
            typer.echo(f"ERROR  {msg}", err=True)
        raise typer.Exit(code=1)
    typer.echo("全部配置通过")


@config_app.command("show")
def config_show() -> None:
    """显示当前配置。"""
    from quant_trading.config import (
        AppSettings,
        load_backtest_config,
        load_pipeline_config,
        load_reporting_config,
        load_universe_config,
    )

    settings = AppSettings()
    typer.echo(f"data_root: {settings.data_root}")
    typer.echo(f"market_mode: {settings.market_mode}")

    pipeline = load_pipeline_config()
    typer.echo(
        f"pipeline: scope={pipeline.collection.scope}"
        f" datasets={pipeline.collection.datasets}"
    )

    universe = load_universe_config()
    typer.echo(f"universes: {sorted(universe.universes)}")

    backtest = load_backtest_config()
    typer.echo(
        f"backtest: initial_cash={backtest.initial_cash:,.0f}"
        f" commission={backtest.commission_rate:.4f}"
    )

    reporting = load_reporting_config()
    typer.echo(f"ai: enabled={reporting.ai.enabled}")


# -- migrate ------------------------------------------------------------


@app.command("migrate")
def migrate(
    source_kind: Annotated[
        Literal["duckdb"], typer.Option("--from", help="当前仅支持 duckdb")
    ] = "duckdb",
    target_kind: Annotated[
        Literal["postgresql"],
        typer.Option("--to", help="当前仅支持 postgresql"),
    ] = "postgresql",
    tables: Annotated[
        str,
        typer.Option(help="逗号分隔的审计表白名单"),
    ] = "data_readiness,etl_run,quality_issue,snapshot_audit",
    dry_run: Annotated[bool, typer.Option(help="只统计，不连接 PostgreSQL")] = False,
    data_root: Annotated[Path | None, typer.Option(help="DuckDB 数据根目录")] = None,
) -> None:
    """将 DuckDB 审计表幂等迁移到 PostgreSQL。"""
    from quant_trading.config import AppSettings
    from quant_trading.data.migration import migrate_audit_tables
    from quant_trading.data.storage import ParquetDuckDBStore, PostgresAuditStore

    del source_kind, target_kind
    settings = AppSettings()
    selected = tuple(item.strip() for item in tables.split(",") if item.strip())
    if not selected:
        raise typer.BadParameter("--tables 不能为空")
    if not dry_run and not settings.postgres_dsn:
        raise typer.BadParameter("实际迁移必须设置 QUANT_POSTGRES_DSN")
    try:
        result = migrate_audit_tables(
            ParquetDuckDBStore(
                data_root or settings.data_root,
                read_only=True,
            ),
            PostgresAuditStore(settings.postgres_dsn or "postgresql://dry-run"),
            selected,
            dry_run=dry_run,
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {"dry_run": result.dry_run, "tables": result.counts},
            ensure_ascii=False,
            indent=2,
        )
    )


# -- version ------------------------------------------------------------


@app.command()
def version() -> None:
    """显示程序版本。"""
    typer.echo(__version__)


if __name__ == "__main__":
    app()

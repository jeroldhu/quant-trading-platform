"""策略注册和配置命令。"""

# TODO(P4-CLI-02): 接入 run/backtest/replay/build-factors 和批量策略运行。
# Contract: docs/development-todo.md#p4-cli-02

from pathlib import Path
from typing import Annotated

import typer

from quant_trading.config import ConfigError, load_strategy_file
from quant_trading.data.readiness import ReadinessGate
from quant_trading.research.strategy_registry import (
    StrategyRegistryError,
    create_strategy,
    list_strategy_names,
)

research_app = typer.Typer(no_args_is_help=True, help="策略与回测命令")
strategies_app = typer.Typer(no_args_is_help=True, help="策略注册与配置命令")
research_app.add_typer(strategies_app, name="strategies")


@strategies_app.command("list")
def strategies_list() -> None:
    """列出代码中显式注册的全部策略。"""

    for name in list_strategy_names():
        typer.echo(name)


@strategies_app.command("validate")
def strategies_validate(
    config_dir: Annotated[
        Path,
        typer.Option(help="策略配置目录"),
    ] = Path("configs/strategies"),
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
        except (ConfigError, StrategyRegistryError, ValueError) as exception:
            errors.append(f"{path}: {exception}")

    if not files:
        errors.append(f"{config_dir}: 没有找到策略 YAML")
    if total_weight > 1.0:
        errors.append(f"启用策略的 capital_weight 合计超过 1: {total_weight:.4f}")
    if errors:
        for message in errors:
            typer.echo(f"ERROR  {message}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"全部通过，共 {len(files)} 个策略配置")

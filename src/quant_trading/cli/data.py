"""数据管道命令。"""

# TODO(P4-CLI-01): 接入全部数据、快照、状态和压缩命令及统一退出码。
# Contract: docs/development-todo.md#p4-cli-01

from pathlib import Path
from typing import Annotated

import typer

from quant_trading.config import AppSettings
from quant_trading.data.pipeline import bootstrap_data_root

data_app = typer.Typer(no_args_is_help=True, help="数据管道命令")


@data_app.command("bootstrap")
def data_bootstrap(
    data_root: Annotated[
        Path | None,
        typer.Option(help="覆盖数据根目录"),
    ] = None,
) -> None:
    """创建数据目录；不会下载行情或写入模拟数据。"""

    root = data_root or AppSettings().data_root
    created = bootstrap_data_root(root)
    typer.echo(f"数据目录已就绪: {root}")
    for path in created:
        typer.echo(f"- {path}")

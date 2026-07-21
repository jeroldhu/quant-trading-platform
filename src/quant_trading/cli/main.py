"""统一命令行入口与子命令装配。"""

# TODO(P4-CLI-03): 装配 scheduler/report/ai/config/migrate 命令和统一错误输出。
# Contract: docs/development-todo.md#p4-cli-03

import typer

from quant_trading import __version__

from .data import data_app
from .research import research_app

app = typer.Typer(no_args_is_help=True, help="A 股量化研究平台")
app.add_typer(data_app, name="data")
app.add_typer(research_app, name="research")


@app.command()
def version() -> None:
    """显示程序版本。"""

    typer.echo(__version__)


if __name__ == "__main__":
    app()

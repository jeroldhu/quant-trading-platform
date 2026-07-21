"""配置读取入口。

配置文件只描述参数，不在加载阶段执行采集、建库等副作用。
"""

# TODO(P1-CONFIG-01): 为全部 YAML 建立严格模型并完成跨字段业务校验。
# Contract: docs/development-todo.md#p1-config-01

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(ValueError):
    """配置文件缺失、格式错误或根节点类型不正确。"""


class AppSettings(BaseSettings):
    """可由环境变量覆盖的应用级设置。"""

    model_config = SettingsConfigDict(
        env_prefix="QUANT_",
        env_file=".env",
        extra="ignore",
    )

    data_root: Path = Path("data")
    snapshot_root: Path = Path("snapshots")
    market_mode: str = "snapshot"
    log_level: str = "INFO"


class StrategyFile(BaseModel):
    """策略 YAML 的公共字段；parameters 由具体策略二次校验。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    version: str
    capital_weight: float = Field(ge=0.0, le=1.0)
    required_readiness: tuple[str, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)


def load_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML，并保证根节点是映射。"""

    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigError(f"YAML 解析失败: {path}") from error

    if not isinstance(loaded, dict):
        raise ConfigError(f"配置根节点必须是映射: {path}")
    return loaded


def load_strategy_file(path: Path) -> StrategyFile:
    """读取策略公共配置，具体参数留给策略自己的配置模型。"""

    return StrategyFile.model_validate(load_yaml(path))

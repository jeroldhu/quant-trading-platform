"""Point-in-time 资产池工具。"""

# TODO(P3-RESEARCH-01): 实现目标日期成员解析、上市退市和流动性过滤。
# Contract: docs/development-todo.md#p3-research-01

from collections.abc import Iterable


def normalize_universe(instruments: Iterable[str]) -> tuple[str, ...]:
    """去重并稳定排序，保证相同输入产生确定结果。"""

    normalized = {instrument.strip().upper() for instrument in instruments}
    return tuple(sorted(instrument for instrument in normalized if instrument))

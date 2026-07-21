"""AI 解释的安全边界。

AI 只能读取已经生成的结果并返回文字解释，不能持有数据写端口或订单端口。
"""

# TODO(P4-AI-01): 实现白名单输入、Prompt 版本、Schema 校验和成本控制。
# Contract: docs/development-todo.md#p4-ai-01

from collections.abc import Mapping
from typing import Protocol


class ReadOnlyEvaluator(Protocol):
    def explain(
        self,
        dimension: str,
        result: Mapping[str, object],
    ) -> Mapping[str, object]: ...

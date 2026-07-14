"""工具调用的串并行计划。"""

from dataclasses import dataclass
from typing import Iterable, Sequence

from app.tools.types import ToolCall, ToolDefinition


@dataclass(frozen=True)
class ToolConcurrencyGroup:
    """表示可并发执行的一组工具调用。

    参数:
        calls: 属于同一执行组的工具调用。
        parallel: 该组是否允许并发执行。
        reason: 分组或串行化原因。

    返回:
        不可变并发分组。

    异常:
        无。

    副作用:
        无。
    """

    calls: Sequence[ToolCall]
    parallel: bool
    reason: str


@dataclass(frozen=True)
class ToolConcurrencyPlan:
    """表示一个模型响应中工具调用的执行计划。

    参数:
        groups: 顺序执行的调用分组。

    返回:
        不可变并发计划。

    异常:
        无。

    副作用:
        无。
    """

    groups: Sequence[ToolConcurrencyGroup]


class ToolConcurrencyPlanner:
    """按声明资源和风险将调用划分为串并行组。"""

    def plan(self, calls: Iterable[ToolCall], definitions: Iterable[ToolDefinition]) -> ToolConcurrencyPlan:
        """为同一轮模型调用构造保守并发计划。

        参数:
            calls: 模型请求的工具调用。
            definitions: 当前注册工具定义。

        返回:
            保持输入顺序、冲突调用被分隔的并发计划。

        异常:
            无。未知工具会在 Runtime 校验阶段处理。

        副作用:
            无。
        """

        definitions_by_name = {definition.name: definition for definition in definitions}
        groups: list[ToolConcurrencyGroup] = []
        parallel_calls: list[ToolCall] = []
        occupied_resources: set[str] = set()
        for call in calls:
            definition = definitions_by_name.get(call.tool_name)
            resources = set(definition.resource_keys) if definition else {f"unknown:{call.tool_name}"}
            is_write_or_conflict = bool(resources & occupied_resources) or any(
                item.startswith(("git:", "pty:")) for item in resources
            )
            if is_write_or_conflict:
                if parallel_calls:
                    groups.append(ToolConcurrencyGroup(tuple(parallel_calls), True, "disjoint resources"))
                    parallel_calls = []
                    occupied_resources = set()
                groups.append(ToolConcurrencyGroup((call,), False, "resource conflict or exclusive resource"))
                continue
            parallel_calls.append(call)
            occupied_resources.update(resources)
        if parallel_calls:
            groups.append(ToolConcurrencyGroup(tuple(parallel_calls), True, "disjoint resources"))
        return ToolConcurrencyPlan(tuple(groups))

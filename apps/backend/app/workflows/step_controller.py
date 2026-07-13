"""可选的、基于 LangGraph 的工作流步骤控制。"""

from dataclasses import dataclass
from importlib import metadata
from typing import Any, Optional, Tuple, TypedDict


MIN_SAFE_LANGGRAPH_VERSION = (1, 0, 10)


class StepGraphState(TypedDict):
    """LangGraph 步骤控制器使用的状态模式。

    参数:
        step_count: 已经尝试过的模型步骤数。
        max_steps: 运行时配置允许的最大模型步骤数。
        can_continue: 工作流是否可以进入下一个模型步骤。

    返回:
        供 LangGraph StateGraph 使用的 TypedDict 模式。

    异常:
        无。

    副作用:
        无。
    """

    step_count: int
    max_steps: int
    can_continue: bool


@dataclass(frozen=True)
class StepDecision:
    """表示一个工作流步骤控制决定。

    参数:
        step_count: 更新后的模型步骤数。
        can_continue: 工作流是否可以运行下一个模型步骤。

    返回:
        不可变的决定值。

    异常:
        无。

    副作用:
        无。
    """

    step_count: int
    can_continue: bool


class LangGraphStepController:
    """在安装了安全版本的 LangGraph 时，通过它推进模型步骤计数。"""

    def __init__(self) -> None:
        """在安装了安全的依赖项时编译 LangGraph 步骤控制器。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果安装了安全的 LangGraph 版本但图编译失败。

        副作用:
            当安全的版本可用时导入并编译一个 LangGraph StateGraph。不安全或缺失的
            版本会使用本地的确定性逻辑。
        """

        self._graph = _build_step_graph()

    @property
    def uses_langgraph(self) -> bool:
        """返回该控制器是否由 LangGraph 支撑。

        参数:
            无。

        返回:
            当安全的 LangGraph 版本被导入并编译时为 True。

        异常:
            无。

        副作用:
            无。
        """

        return self._graph is not None

    async def next_step(self, step_count: int, max_steps: int) -> StepDecision:
        """返回下一个工作流步骤决定。

        参数:
            step_count: 已经尝试过的当前模型步骤数。
            max_steps: 允许的最大模型步骤数。

        返回:
            带有更新后的步骤数与继续标志的 StepDecision。

        异常:
            Exception: 如果已编译的 LangGraph 执行失败。

        副作用:
            在可用时执行已编译的 LangGraph。
        """

        initial_state: StepGraphState = {
            "step_count": step_count,
            "max_steps": max_steps,
            "can_continue": False,
        }
        if self._graph is None:
            result = _advance_step(initial_state)
        else:
            result = await self._graph.ainvoke(initial_state)
        return StepDecision(
            step_count=result["step_count"],
            can_continue=result["can_continue"],
        )


def _build_step_graph() -> Optional[Any]:
    """构建并编译 LangGraph 步骤控制器。

    参数:
        无。

    返回:
        当安装了安全的依赖项时返回已编译的 LangGraph 对象，否则返回 None。

    异常:
        RuntimeError: 如果安装了安全的 LangGraph 版本但无法编译该图。

    副作用:
        当安全的版本可用时导入 LangGraph 并编译一个 StateGraph。
    """

    version = _installed_langgraph_version()
    if version is None or not _version_at_least(version, MIN_SAFE_LANGGRAPH_VERSION):
        return None

    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise RuntimeError("safe LangGraph version is installed but cannot be imported") from exc

    graph = StateGraph(StepGraphState)
    graph.add_node("advance_step", _advance_step)
    graph.add_edge(START, "advance_step")
    graph.add_edge("advance_step", END)
    return graph.compile()


def _installed_langgraph_version() -> Optional[Tuple[int, int, int]]:
    """将已安装的 LangGraph 版本作为可比较的元组返回。

    参数:
        无。

    返回:
        当 LangGraph 已安装时为 ``(major, minor, patch)``，否则为 None。

    异常:
        无。无效的版本文本会被视为不可用。

    副作用:
        读取已安装的包元数据。
    """

    try:
        raw_version = metadata.version("langgraph")
    except metadata.PackageNotFoundError:
        return None
    parts = raw_version.split(".")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2].split("-")[0]))
    except (IndexError, ValueError):
        return None


def _version_at_least(version: Tuple[int, int, int], minimum: Tuple[int, int, int]) -> bool:
    """返回版本元组是否满足最低版本要求。

    参数:
        version: 已安装的版本元组。
        minimum: 要求的最低版本元组。

    返回:
        当 ``version`` 大于或等于 ``minimum`` 时为 True。

    异常:
        无。

    副作用:
        无。
    """

    return version >= minimum


def _advance_step(state: StepGraphState) -> StepGraphState:
    """如果工作流可以继续，则推进模型步骤计数。

    参数:
        state: 当前的步骤图状态。

    返回:
        更新后的步骤图状态。

    异常:
        无。

    副作用:
        无。
    """

    if state["step_count"] >= state["max_steps"]:
        return {
            "step_count": state["step_count"],
            "max_steps": state["max_steps"],
            "can_continue": False,
        }
    return {
        "step_count": state["step_count"] + 1,
        "max_steps": state["max_steps"],
        "can_continue": True,
    }

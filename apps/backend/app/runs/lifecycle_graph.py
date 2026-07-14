"""Durable Run 生命周期 LangGraph 构建器。"""

from typing import Any, Dict, TypedDict


class RunLifecycleState(TypedDict, total=False):
    """LangGraph checkpoint 中保存的运行生命周期状态。

    参数:
        run_id: Durable Run 标识符。
        task_id: 关联任务标识符。
        phase: 当前生命周期阶段。
        task_status: 当前任务状态。
        payload: 附加事件载荷。

    返回:
        可被 LangGraph StateGraph 持久化的字典状态。

    异常:
        无。

    副作用:
        无。
    """

    run_id: str
    task_id: str
    phase: str
    task_status: str
    payload: Dict[str, Any]


def build_run_lifecycle_graph(checkpointer: Any) -> Any:
    """构建记录 Durable Run 生命周期的 LangGraph。

    参数:
        checkpointer: LangGraph 官方 checkpointer 或兼容包装对象。

    返回:
        已绑定 checkpointer 的 compiled graph。

    异常:
        RuntimeError: 如果已安装的 LangGraph 无法导入 StateGraph。

    副作用:
        导入 LangGraph，并编译一个会在每次 invoke 时写入 checkpoint 的图。
    """

    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise RuntimeError("无法导入 LangGraph StateGraph") from exc

    graph = StateGraph(RunLifecycleState)
    graph.add_node("record_lifecycle", record_lifecycle_phase)
    graph.add_edge(START, "record_lifecycle")
    graph.add_edge("record_lifecycle", END)
    return graph.compile(checkpointer=checkpointer)


def record_lifecycle_phase(state: RunLifecycleState) -> RunLifecycleState:
    """返回需要写入 checkpoint 的运行生命周期状态，并在审批等待点中断。

    参数:
        state: 本次 graph invoke 传入的运行生命周期状态。

    返回:
        普通阶段原样返回；审批等待阶段在恢复后返回包含 resume 值的状态。

    异常:
        RuntimeError: 如果当前环境无法导入 LangGraph interrupt。

    副作用:
        当 phase 为 waiting_approval 时触发 LangGraph interrupt，暂停当前 thread。
    """

    if state.get("phase") == "waiting_approval":
        payload = state.get("payload", {})
        return {
            **state,
            "phase": "approval_resumed",
            "payload": {
                **payload,
                "resume": wait_for_human_approval(payload),
            },
        }
    return state


def wait_for_human_approval(payload: Dict[str, Any]) -> Any:
    """通过 LangGraph interrupt 等待人类审批结果。

    参数:
        payload: 展示给人类审批界面的审批请求载荷。

    返回:
        graph 被 ``Command(resume=...)`` 恢复时传入的值。

    异常:
        RuntimeError: 如果当前环境无法导入 LangGraph interrupt。

    副作用:
        调用 LangGraph interrupt，暂停当前 graph thread 并持久化 checkpoint。
    """

    try:
        from langgraph.types import interrupt
    except ImportError as exc:
        raise RuntimeError("无法导入 LangGraph interrupt") from exc
    return interrupt(payload)

"""LangGraph 调用与恢复命令适配。"""

from typing import Any, Optional

from app.runs.checkpointer import build_thread_config


def build_resume_command(value: Any) -> Any:
    """构建 LangGraph Command(resume=...) 对象。

    参数:
        value: 需要传回 interrupt 的恢复值。

    返回:
        LangGraph Command 对象。

    异常:
        RuntimeError: 如果当前环境未安装 LangGraph。

    副作用:
        导入 LangGraph 类型。
    """

    try:
        from langgraph.types import Command
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 langgraph，无法构建恢复命令") from exc
    return Command(resume=value)


def invoke_graph(graph: Any, thread_id: str, input_value: Optional[Any] = None, resume_value: Optional[Any] = None) -> Any:
    """使用稳定 thread_id 调用或恢复 LangGraph graph。

    参数:
        graph: 已编译的 LangGraph graph。
        thread_id: Durable Run 绑定的 thread_id。
        input_value: 初次调用 graph 的输入值。
        resume_value: 恢复 interrupt 时传入的值。

    返回:
        LangGraph invoke 返回值。

    异常:
        ValueError: 如果同时提供 input_value 和 resume_value。
        AttributeError: 如果 graph 不支持 invoke。

    副作用:
        调用 LangGraph graph，可能推进持久化状态。
    """

    if input_value is not None and resume_value is not None:
        raise ValueError("input_value and resume_value cannot both be provided")
    payload = build_resume_command(resume_value) if resume_value is not None else input_value
    return graph.invoke(payload, config=build_thread_config(thread_id))

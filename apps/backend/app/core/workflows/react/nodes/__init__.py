# noinspection PyProtectedMember
"""ReAct-like 工作流的节点行为子包。

本包分两层，节点与共享辅助不混放：

``nodes/``（注册进 graph 的 LangGraph 节点，每文件一节点）：
- ``model_node``：``model`` 节点（``_model_node``）——流式消费模型输出、决定走向，并在
  检测到协作取消时经 ``interrupt`` 中断图。
- ``tools_node``：``tools`` 节点（``_tools_node``）——执行本批尚未起跑（``pending``）的工具调用，
  并登记观察摘要与终端会话元数据；不做投影、不做终态事件与终态判定（待决请求随观察本身携带，
  由 ``wait_user`` 派生）。
- ``wait_user_node``：``wait_user`` 节点（``wait_user_node``）——human-in-the-loop 门：挂起前把
  待决请求投影给前端、按待决请求经 ``interrupt`` 挂起、消费用户决定并据此重置执行门控或收口观察。
- ``observation_node``：``observe`` 节点（``_observe_node``）——工具结果观察处理的单一收口：
  终态事件分发、``ToolMessage`` 写回、连续失败计数与错误上限判定（阶段二将在此接入 LLM 观察推理）。

``node_helper/``（节点共享的辅助，不注册为图节点）：
- ``model_chunk``：模型流式 chunk 解析（``ModelChunkProcessor``：思考抽取 + 工具调用提前抽取
  + 完成原因归一化）。
- ``streaming_part_state_machine``：text / reasoning 增量合并与 part 收口。
- ``tool_call_lifecycle``：工具调用生命周期快照、起跑边界与终态分发。
- ``user_input_projection``：human-in-the-loop 的纯投影函数（待决请求派生、请求 ↔ 调用关联、
  恢复值解析、决定写回观察）。
- ``finalize_max_steps``：超步数终态收口（被 ``model_node`` 直接调用的函数，非图节点）。
- ``common``：节点共享的运行时原语（runtime config / context 取出、动态路由 state 构造）。
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 仅为静态分析（IDE / mypy）提供名称声明：``__all__`` 中的名字由下方 ``__getattr__``
    # 惰性返回，静态分析看不到 ⇒ 会报「未解析的引用」。本分支运行时不执行，惰性加载语义不变。
    from app.core.workflows.react.node_helper import _finalize_max_steps
    from app.core.workflows.react.nodes.model_node import _model_node
    from app.core.workflows.react.nodes.observation_node import _observe_node
    from app.core.workflows.react.nodes.structured_output_node import _structured_output_node
    from app.core.workflows.react.nodes.tools_node import _tools_node


def __getattr__(name: str) -> Any:
    """按需加载节点，避免 state 类型导入触发节点包级循环。"""

    if name == "_finalize_max_steps":
        from app.core.workflows.react.node_helper import _finalize_max_steps

        return _finalize_max_steps
    if name == "_model_node":
        from app.core.workflows.react.nodes.model_node import _model_node

        return _model_node
    if name == "_observe_node":
        from app.core.workflows.react.nodes.observation_node import _observe_node

        return _observe_node
    if name == "_structured_output_node":
        from app.core.workflows.react.nodes.structured_output_node import (
            _structured_output_node,
        )

        return _structured_output_node
    if name == "_tools_node":
        from app.core.workflows.react.nodes.tools_node import _tools_node

        return _tools_node
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "_finalize_max_steps",
    "_model_node",
    "_observe_node",
    "_structured_output_node",
    "_tools_node",
]

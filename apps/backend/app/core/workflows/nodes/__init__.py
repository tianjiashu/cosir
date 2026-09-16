# noinspection PyProtectedMember
"""ReAct-like 工作流的节点行为子包。

本包分两层，节点与共享辅助不混放：

``nodes/``（注册进 graph 的 LangGraph 节点，每文件一节点）：
- ``model_node``：``model`` 节点（``_model_node``）及其强绑定辅助（成本估算 / 上下文占用
  事件）。
- ``tools_node``：``tools`` 节点（``_tools_node``）及工具批次执行与执行前取消检查。
- ``observation_node``：``observe`` 节点（``_observe_node``）——协作取消的唯一收口点；
  从 ``last_tool_results`` 重算连续失败计数并判定错误上限（阶段二将在此接入 LLM 观察推理）。
- ``pause_node``：``pause`` 节点（``_pause_node``），协作取消的暂停点（图停在此处并保留
  ``next``，使该 run 可被续跑）。

``nodes/helper/``（节点共享的辅助，不注册为图节点）：
- ``model_chunk``：模型流式 chunk 解析（``ModelChunkProcessor``：思考抽取 + 工具调用提前抽取
  + chunk 合并为 ``AIMessage``）。
- ``streaming_part_state_machine``：text / reasoning 增量合并与 part 收口。
- ``tool_call_lifecycle``：工具调用生命周期快照、终态分发与非法调用修复提示构造。
- ``finalize_max_steps``：超步数终态收口。
- ``debug_dump``：模型 chunk 调试落盘（``logs/debug_merged_chunks.jsonl`` 等）。
- ``common``：节点共享的运行时原语（runtime config / context 取出、``terminal_state``）。
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 仅为静态分析（IDE / mypy）提供名称声明：``__all__`` 中的名字由下方 ``__getattr__``
    # 惰性返回，静态分析看不到 ⇒ 会报「未解析的引用」。本分支运行时不执行，惰性加载语义不变。
    from app.core.workflows.nodes.helper.finalize_max_steps import _finalize_max_steps
    from app.core.workflows.nodes.model_node import _model_node
    from app.core.workflows.nodes.observation_node import _observe_node
    from app.core.workflows.nodes.pause_node import _pause_node
    from app.core.workflows.nodes.tools_node import _tools_node


def __getattr__(name: str) -> Any:
    """按需加载节点，避免 state 类型导入触发节点包级循环。"""

    if name == "_finalize_max_steps":
        from app.core.workflows.nodes.helper.finalize_max_steps import _finalize_max_steps

        return _finalize_max_steps
    if name == "_pause_node":
        from app.core.workflows.nodes.pause_node import _pause_node

        return _pause_node
    if name == "_model_node":
        from app.core.workflows.nodes.model_node import _model_node

        return _model_node
    if name == "_observe_node":
        from app.core.workflows.nodes.observation_node import _observe_node

        return _observe_node
    if name == "_tools_node":
        from app.core.workflows.nodes.tools_node import _tools_node

        return _tools_node
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "_finalize_max_steps",
    "_model_node",
    "_observe_node",
    "_pause_node",
    "_tools_node",
]

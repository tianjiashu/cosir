"""ReAct-like 工作流的节点行为子包。

职责划分（每文件一职责）：
- ``model_node``：``model`` 节点（``_model_node``）及其强绑定辅助（成本估算 / 上下文占用
  事件）。
- ``model_chunk``：模型流式 chunk 解析（``ModelChunkProcessor``：思考抽取 + 工具调用提前抽取
  + chunk 合并为 ``AIMessage``）。
- ``debug_dump``：模型 chunk 调试落盘（``logs/debug_merged_chunks.jsonl`` 等）。
- ``tools_node``：``tools`` 节点（``_tools_node``）及工具观察的增量落库/写回
  （``_persist_tool_observations``）。
- ``observation_node``：``observe`` 节点（``_observe_node``），从 ``last_tool_results``
  重算连续失败计数并判定错误上限（阶段二将在此接入 LLM 观察推理）。
- ``common``：节点共享的运行时原语（事件写入、config/context 取出）。
"""

from typing import Any


def __getattr__(name: str) -> Any:
    """按需加载节点，避免 state 类型导入触发节点包级循环。"""

    if name == "_finalize_max_steps":
        from app.core.workflows.nodes.helper.finalize_max_steps import _finalize_max_steps

        return _finalize_max_steps
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
    "_tools_node",
]

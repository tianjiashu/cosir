"""ReAct-like 工作流的节点行为子包。

职责划分（每文件一职责）：
- ``model_node``：``model`` 节点（``_model_node``）及其强绑定辅助（成本估算 / 上下文占用
  事件）。
- ``thinking_extractor``：思考通道抽取纯函数（厂商 thinking 抽取 + 回传剥离策略）。
- ``chunk_assembler``：chunk 组装（``AIMessageChunk`` 列表 → ``AIMessage`` + 文本 + usage）。
- ``debug_dump``：模型 chunk 调试落盘（``logs/debug_merged_chunks.jsonl`` 等）。
- ``invalid_tool_call``：非法工具调用的纯决策与修复提示构造（``InvalidToolOutcome`` +
  模块级纯函数）。
- ``tools_node``：``tools`` 节点（``_tools_node``）及工具观察的增量落库/写回
  （``_persist_tool_observations``）。
- ``observation_node``：``observe`` 节点（``_observe_node``），从 ``last_tool_results``
  重算连续失败计数并判定错误上限（阶段二将在此接入 LLM 观察推理）。
- ``common``：节点共享的运行时原语（事件写入、config/context 取出）。
"""

from app.core.workflows.nodes.helper.finalize_max_steps import _finalize_max_steps
from app.core.workflows.nodes.model_node import _model_node
from app.core.workflows.nodes.observation_node import _observe_node
from app.core.workflows.nodes.tools_node import _tools_node

__all__ = [
    "_finalize_max_steps",
    "_model_node",
    "_observe_node",
    "_tools_node",
]

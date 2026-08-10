"""ReAct-like 工作流的节点行为子包。

职责划分（每文件一职责）：
- ``model_node``：``model`` 节点（``_model_node``）及其模型侧数据处理辅助。
- ``tools_node``：``tools`` 节点（``_tools_node``）及工具观察的
  增量落库/写回（``_persist_tool_observations``）。
- ``observation_node``：``observe`` 节点（``_observe_node``），从 ``last_tool_results``
  重算连续失败计数并判定错误上限（阶段二将在此接入 LLM 观察推理）。
- ``common``：节点共享的运行时原语（事件写入、config/context 取出）。
"""

from app.core.workflows.nodes.model_node import _model_node
from app.core.workflows.nodes.observation_node import _observe_node
from app.core.workflows.nodes.tools_node import _tools_node

__all__ = [
    "_model_node",
    "_observe_node",
    "_tools_node",
]

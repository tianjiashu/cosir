"""ReAct-like 工作流模块入口，向后兼容 re-export。

历史代码从 ``app.core.workflows.react.react_like`` 导入本模块的符号。实际实现已按职责
拆分到 ``state`` / ``nodes`` / ``edges`` / ``workflow`` 子模块，本文件仅保留为薄
re-export 壳，避免改动既有调用方与测试。
"""

from app.core.workflows.react.edges import _should_continue
from app.core.workflows.react.nodes import _model_node, _tools_node
from app.core.workflows.react.state import ReactGraphState, _add_messages
from app.core.workflows.react.workflow import ReactLikeWorkflow

__all__ = [
    "ReactGraphState",
    "ReactLikeWorkflow",
    "_add_messages",
    "_model_node",
    "_should_continue",
    "_tools_node",
]

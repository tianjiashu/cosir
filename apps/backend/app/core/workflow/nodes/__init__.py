# noinspection PyProtectedMember
"""ReAct 步骤实现。步骤返回下一步的局部更新，loop 统一持久化 Run 进度。"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 仅为静态分析（IDE / mypy）提供名称声明：``__all__`` 中的名字由下方 ``__getattr__``
    # 惰性返回，静态分析看不到 ⇒ 会报「未解析的引用」。本分支运行时不执行，惰性加载语义不变。
    from app.core.workflow.node_helper.finalize_max_steps import _finalize_max_steps
    from app.core.workflow.nodes.model_node import _model_node
    from app.core.workflow.nodes.observation_node import _observe_node
    from app.core.workflow.nodes.structured_output_node import _structured_output_node
    from app.core.workflow.nodes.tools_node import _tools_node


def __getattr__(name: str) -> Any:
    """按需加载节点，避免 state 类型导入触发节点包级循环。"""

    if name == "_finalize_max_steps":
        from app.core.workflow.node_helper.finalize_max_steps import _finalize_max_steps

        return _finalize_max_steps
    if name == "_model_node":
        from app.core.workflow.nodes.model_node import _model_node

        return _model_node
    if name == "_observe_node":
        from app.core.workflow.nodes.observation_node import _observe_node

        return _observe_node
    if name == "_structured_output_node":
        from app.core.workflow.nodes.structured_output_node import (
            _structured_output_node,
        )

        return _structured_output_node
    if name == "_tools_node":
        from app.core.workflow.nodes.tools_node import _tools_node

        return _tools_node
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "_finalize_max_steps",
    "_model_node",
    "_observe_node",
    "_structured_output_node",
    "_tools_node",
]

"""ReAct-like 工作流节点间共享的运行时辅助。

本模块只承载「各节点按需复用」的公共原语，不包含任何单节点专属逻辑（``_runtime_config``
为 model / tools / observe 与工具调用生命周期共用；``_runtime_context`` 为 model / observe
与工具调用生命周期共用；``end_state`` 为 model 与超步数收口共用）：

- ``_runtime_config`` / ``_runtime_context``：从 LangGraph 运行上下文取运行时配置与
  task 级上下文。
- ``end_state``：统一构造结束路由 patch，避免重复书写 graph 控制字段。

节点各自的数据处理辅助不放这里；``content → text`` 归一统一收口于
``app.utils.message_content.content_to_text``（原 AIMessageChunk 抽取与 runtime_context_manager
两套同构口径已合并至此）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.config import get_config

from app.core.workflows.react.worflow_state.route import ReactRoute

if TYPE_CHECKING:
    from app.core.context.runtime_context_manager import RuntimeContextManager
    from app.core.workflows.react.runtime_config import RuntimeConfig


def _runtime_config() -> RuntimeConfig:
    """从 LangGraph 运行上下文取出 ReAct 工作流注入的运行时配置容器。

    ``ReactLikeWorkflow.run()`` 把 ``RuntimeConfig`` 放入 config 的 ``runtime_config``；
    节点统一经本函数取出，避免在各节点里用裸字符串 key 重复读取 ``config["configurable"]``。

    返回:
        当前 graph 执行注入的 ``RuntimeConfig`` 实例。
    """
    # 从 LangGraph 注入的 config 中取出预先放好的 RuntimeConfig。
    return get_config()["configurable"]["runtime_config"]


def _runtime_context() -> RuntimeContextManager:
    """从 LangGraph 运行上下文取出 task 级运行时上下文。

    ``ReactLikeWorkflow.run()`` 把 ``RuntimeContextManager`` 放入 config 的 ``runtime_context``；
    节点统一经本函数取出，与 ``_runtime_config`` 同口径，避免裸字符串 key 重复读取
    ``config["configurable"]``，且使上下文对象不进入 graph state（不兼容消息 reducer）。

    返回:
        当前 graph 执行注入的 ``RuntimeContextManager`` 实例。
    """
    # 从 LangGraph 注入的 config 中取出预先放好的 RuntimeContextManager。
    return get_config()["configurable"]["runtime_context"]


def end_state(step_count: int) -> dict[str, Any]:
    """构造结束当前 graph 执行的 state patch。

    Run 的完成、失败或取消已由 canonical writer 记录；本函数只更新图步数和下一路由，
    不写入第二套 Run 生命周期状态。

    参数:
        step_count: 当前步编号，写入 graph state。

    返回:
        可直接返回给 LangGraph 合并的结束路由 patch；需要附加终态文本的调用方可叠加字段。

    异常:
        无。

    副作用:
        无。
    """
    return {
        "step_count": step_count,
        "next_node": ReactRoute.END,
    }

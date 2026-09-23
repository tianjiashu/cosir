"""ReAct-like 工作流节点间共享的运行时辅助。

本模块只承载「各节点按需复用」的公共原语，不包含任何单节点专属逻辑（``_runtime_config``
为 model / tools / observe 与工具调用生命周期共用；``_runtime_context`` 为 model / observe
与工具调用生命周期共用；``terminal_state`` 为 model 与超步数收口共用）：

- ``_runtime_config`` / ``_runtime_context``：从 LangGraph 运行上下文取运行时配置与
  task 级上下文。
- ``terminal_state``：统一构造终态 state patch_write，消除各节点
  重复的 ``{"terminal": True, ...}`` 字典字面量。

节点各自的数据处理辅助不放这里；``content → text`` 归一统一收口于
``app.utils.message_content.content_to_text``（原 AIMessageChunk 抽取与 runtime_context_manager
两套同构口径已合并至此）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.config import get_config

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


def terminal_state(
    step_count: int,
    *,
    requested_tool: bool = False,
    final_response: bool = False,
) -> dict[str, Any]:
    """构造统一的终态 state patch_write（graph 走到 END 用）。

    ``model`` 节点的各终态分支与 ``_finalize_max_steps`` 都要写同一组硬字段
    （``step_count`` / ``requested_tool`` / ``final_response`` / ``terminal``），手写易错且
    各处分歧；本函数把它收口为单一来源。

    参数:
        step_count: 当前步编号，直接落入 patch_write。
        requested_tool: 本步是否请求了工具，默认 ``False``。
        final_response: 是否产出终态文本，默认 ``False``。

    返回:
        可直接 ``return`` 给 LangGraph 合并的 state patch_write 字典（``terminal`` 恒为
        ``True``）；需要附加终态字段（如 ``final_text``）的调用方在其结果上叠加。

    异常:
        无。

    副作用:
        无。
    """
    return {
        "step_count": step_count,
        "requested_tool": requested_tool,
        "final_response": final_response,
        "terminal": True,
    }

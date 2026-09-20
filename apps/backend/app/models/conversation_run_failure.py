"""Conversation Run 失败的稳定 code 与面向用户的受控文案目录。

单一职责：维护「Run 失败 code → 受控用户文案」这一张表，并声明不属于模型调用的流程失败 code。

模型调用错误的分类词表是 ``ErrorKind``（``app/models/enums/error_kind.py``）：本模块**不再
另行定义** ``model_*`` 系列 code，只用 ``ErrorKind`` 成员的稳定字符串值作为文案键，避免同一
语义出现两套并行事实源。

不负责：把异常归类为 code（见 ``app.core.llm_provider.model_failure``）、状态迁移与持久化
（见 ``ConversationRunStateService``）、Transport 投影与渲染（见 ``app.assistant_transport.state``
与前端 renderer）。

设计约束：
- 文案必须与 provider 解耦：主语统一用「模型服务」这类通用表述，不出现厂商名、模型名、
  HTTP 状态码或原始报文片段。provider 原始异常只允许进入日志与模型通道。
- 同一 code 同时充当 ``conversation_runs.end_reason`` 与 ``ConversationRunError.code``，
  故本模块只维护一份标识，避免两处漂移。
- 未知 code 一律回退到通用文案，保证 UI 永远拿得到可展示文本。
"""

from __future__ import annotations

from app.models.enums.error_kind import ErrorKind

# 兜底 code：没有任何可用分类信息时使用。
RUN_FAILURE_CODE_UNKNOWN = "run_failed"
# 取消类终态：领域侧未给出具体取消原因时的兜底 code。
RUN_FAILURE_CODE_CANCELLED = "run_cancelled"
# 配置期失败：provider / 模型解析不出可用模型，或 Run 缺少 provider 绑定。
RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE = "model_resolve_failed"
# 流程类失败：与模型调用错误无关，出处在 workflow 节点或生命周期收口。
RUN_FAILURE_CODE_MODEL_OUTPUT_INVALID = "invalid_model_output"
RUN_FAILURE_CODE_TOOL_ERROR_LIMIT = "tool_error_limit_reached"
RUN_FAILURE_CODE_MAX_STEPS = "max_steps_reached"
RUN_FAILURE_CODE_GRAPH_FAILED = "workflow_graph_failed"
RUN_FAILURE_CODE_GRAPH_ALREADY_FINISHED = "graph_already_finished"
RUN_FAILURE_CODE_BACKEND_RESTARTED = "backend_restarted"
RUN_FAILURE_CODE_CLIENT_DISCONNECTED = "client_disconnected"

_DEFAULT_MESSAGE = "对话运行失败，请重试或查看日志"

# 唯一文案目录：键为失败 code，值为面向用户的受控文案。
_RUN_FAILURE_MESSAGES: dict[str, str] = {
    RUN_FAILURE_CODE_UNKNOWN: _DEFAULT_MESSAGE,
    RUN_FAILURE_CODE_CANCELLED: "已取消本轮对话",
    RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE: "模型配置不可用，请检查模型设置后重试",
    RUN_FAILURE_CODE_MODEL_OUTPUT_INVALID: "模型未返回可用内容，请重试",
    RUN_FAILURE_CODE_TOOL_ERROR_LIMIT: "连续工具调用失败过多，已停止本轮对话",
    RUN_FAILURE_CODE_MAX_STEPS: "已达最大步数上限，已停止本轮对话",
    RUN_FAILURE_CODE_GRAPH_FAILED: "对话运行中断，请重试",
    RUN_FAILURE_CODE_GRAPH_ALREADY_FINISHED: "本轮对话已结束，无法继续，请新建对话",
    RUN_FAILURE_CODE_BACKEND_RESTARTED: "后端已重启，本轮对话被中断，请重新发送",
    RUN_FAILURE_CODE_CLIENT_DISCONNECTED: "连接已断开，本轮对话被中断，请重新发送",
    # 取消类：领域侧只给到这些原因时的通用文案。
    "user_cancelled": "已取消本轮对话",
    "runtime_cancelled": "已取消本轮对话",
    # 模型调用错误：code 来自 ErrorKind（唯一分类词表），此处只登记文案。
    ErrorKind.MODEL_AUTH_FAILED.value: "模型服务鉴权失败，请检查 API Key 配置",
    ErrorKind.MODEL_INSUFFICIENT_QUOTA.value: "模型服务配额或余额不足，请充值或更换模型",
    ErrorKind.MODEL_RATE_LIMITED.value: "模型服务请求过于频繁，请稍后重试",
    ErrorKind.MODEL_TIMEOUT.value: "模型服务响应超时，请稍后重试",
    ErrorKind.MODEL_NETWORK_ERROR.value: "无法连接模型服务，请检查网络或代理设置",
    ErrorKind.MODEL_SERVICE_ERROR.value: "模型服务暂时不可用，请稍后重试",
    ErrorKind.MODEL_NOT_FOUND.value: "所选模型当前不可用，请在模型设置中重新选择",
    ErrorKind.MODEL_CONTEXT_WINDOW_EXCEEDED.value: "对话上下文过长，请新建对话或精简内容后重试",
    ErrorKind.MODEL_INVALID_REQUEST.value: "请求被模型服务拒绝，请检查模型设置或精简输入后重试",
    ErrorKind.MODEL_CONTENT_BLOCKED.value: "内容被模型服务拦截，请调整输入后重试",
    ErrorKind.MODEL_UNKNOWN.value: "模型服务返回未知错误，请重试",
}


def run_failure_message(code: str | None) -> str:
    """把 Run 失败 code 映射为面向用户的受控文案。

    参数:
        code: 失败 code（通常来自 ``conversation_runs.end_reason``，取值是流程 code 常量或
            ``ErrorKind`` 的 ``model_*`` 值）；为 ``None`` 或未登记时使用通用兜底文案。

    返回:
        非空的中文受控文案；保证不含 provider 名称、模型名、原始报文或状态码。

    异常:
        无。

    副作用:
        无（纯函数）。
    """

    if code is None:
        return _DEFAULT_MESSAGE
    return _RUN_FAILURE_MESSAGES.get(code, _DEFAULT_MESSAGE)


__all__ = [
    "RUN_FAILURE_CODE_BACKEND_RESTARTED",
    "RUN_FAILURE_CODE_CANCELLED",
    "RUN_FAILURE_CODE_CLIENT_DISCONNECTED",
    "RUN_FAILURE_CODE_GRAPH_ALREADY_FINISHED",
    "RUN_FAILURE_CODE_GRAPH_FAILED",
    "RUN_FAILURE_CODE_MAX_STEPS",
    "RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE",
    "RUN_FAILURE_CODE_MODEL_OUTPUT_INVALID",
    "RUN_FAILURE_CODE_TOOL_ERROR_LIMIT",
    "RUN_FAILURE_CODE_UNKNOWN",
    "run_failure_message",
]

"""模型探活失败的受控文案目录。

单一职责：把 :class:`ModelProbeFailureKind` 映射为面向前端与用户的受控短文案。
该字典是模型配置测试失败文案的**唯一目录**——新增失败分类时必须在此补文案，否则会回退到
通用文案（而不是把原始异常、URL 响应正文或供应商报文透给用户）。

职责边界：
- 负责：失败分类 → 文案的稳定映射。
- 不负责：失败分类的产生（见 ``app/core/llm_provider/model_probe.py``）、日志与结果对象
  组装（见 ``ModelConfigService``）。
"""

from __future__ import annotations

from app.core.llm_provider.model_probe import ModelProbeFailureKind

__all__ = ["probe_failure_message"]

# 键为探活失败分类，值为面向用户的受控文案；文案不含 Key、原始异常正文或供应商报文。
_PROBE_FAILURE_MESSAGES: dict[ModelProbeFailureKind, str] = {
    ModelProbeFailureKind.INVALID_CONFIG: "模型配置不完整，请填写模型名称与上下文窗口",
    ModelProbeFailureKind.AUTH_FAILED: "鉴权失败，请检查 API Key 是否正确且有权访问该模型",
    ModelProbeFailureKind.ENDPOINT_NOT_FOUND: (
        "端点不存在，请检查 Base URL 是否为 OpenAI 兼容接口（通常形如 /v1）"
    ),
    ModelProbeFailureKind.REQUEST_REJECTED: "请求被拒绝，请检查模型名称与参数是否被该端点支持",
    ModelProbeFailureKind.RATE_LIMITED: "触发限流或额度不足，请稍后重试或检查账户额度",
    ModelProbeFailureKind.TIMEOUT: "连接超时，请检查网络、代理或端点可用性",
    ModelProbeFailureKind.NETWORK_ERROR: "网络连接失败，请检查网络、代理或端点地址",
    ModelProbeFailureKind.INVALID_RESPONSE: (
        "端点未返回可用的模型响应，请检查 Base URL 是否为 OpenAI 兼容接口"
    ),
    ModelProbeFailureKind.UPSTREAM_ERROR: "上游服务异常，请稍后重试",
    ModelProbeFailureKind.UNKNOWN: "连接失败，请检查 Base URL、API Key 和模型名称",
}

_FALLBACK_MESSAGE = _PROBE_FAILURE_MESSAGES[ModelProbeFailureKind.UNKNOWN]


def probe_failure_message(kind: ModelProbeFailureKind | None) -> str:
    """返回失败分类对应的受控文案。

    参数:
        kind: 探活失败分类；``None``（未分类）与未知取值都回退到通用文案。

    返回:
        可直接展示给用户的短文案（不含敏感信息）。

    异常:
        无。

    副作用:
        无。
    """

    if kind is None:
        return _FALLBACK_MESSAGE
    return _PROBE_FAILURE_MESSAGES.get(kind, _FALLBACK_MESSAGE)

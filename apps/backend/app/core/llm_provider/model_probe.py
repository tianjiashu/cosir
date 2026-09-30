"""模型连接真实探活：复用运行期模型构建入口做一次最小流式调用。

单一职责：给定一份**已物化**的 ``ModelSettings``，用运行期同一构建入口（``build_chat_model``）
发起一次最小流式请求，判定该配置能否产出生成内容，并把失败归一为稳定的
:class:`ModelProbeFailureKind`。

职责边界：
- 负责：构建模型、读取首个生成 chunk、超时控制、失败分类、失败日志。
- 不负责：读取配置数据库（设置由调用方物化后传入）、面向用户的失败文案（由 service 层的
  受控文案目录映射）、Run 生命周期与任何持久化。

为什么必须复用 ``build_chat_model``：连接测试与运行期一旦各写一套请求实现，URL 拼接、流式
参数与成功判据就会分叉。历史事故即为实例——「非流式 + 只看 HTTP 状态码」把返回网站首页
（200 + HTML）的错误端点判为连接成功，而运行期因为流里没有任何 chunk 直接判失败。

有意保留的差异（契约，不是缺陷）：
- 不绑定工具、不带多轮上下文；
- 强制 ``max_tokens`` 为最小值（默认 1），读到首个 chunk 即结束，控制耗时与成本；
- 默认不注入 ``reasoning_effort`` 偏好：连接测试只验证「端点 + 模型 + 流式通道」可用，
  不验证推理强度参数；需要时可显式传入。

异常：
    本模块不向调用方抛业务异常——模型构建期与请求期的失败都被归一为
    :class:`ModelProbeOutcome`；仅 ``asyncio.CancelledError`` 按原语义向上传播。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from enum import Enum
from time import perf_counter

import httpx
import openai
from langchain_core.messages import HumanMessage

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.model_settings import ModelSettings, ModelSettingsError
from app.core.llm_provider.model_factory import build_chat_model

__all__ = [
    "ModelProbeFailureKind",
    "ModelProbeOutcome",
    "probe_chat_model",
]


class ModelProbeFailureKind(str, Enum):
    """探活失败的稳定分类。

    取值直接落日志并透出给调用方做受控文案映射，因此不得随意改名；新增分类应同时更新
    service 层的文案目录，避免出现没有文案的裸 code。
    """

    # 设置未物化或基础字段非法（缺 model_name / context_window_k 等），请求未发出。
    INVALID_CONFIG = "invalid_config"
    # 鉴权失败：Key 缺失、错误或无权访问该模型。
    AUTH_FAILED = "auth_failed"
    # 端点不存在：Base URL 路径不对（如该带 /v1 而未带）或路由未实现。
    ENDPOINT_NOT_FOUND = "endpoint_not_found"
    # 请求被端点拒绝：模型名不存在、参数不被支持等。
    REQUEST_REJECTED = "request_rejected"
    # 触发限流或额度耗尽。
    RATE_LIMITED = "rate_limited"
    # 请求超时。
    TIMEOUT = "timeout"
    # 网络层失败：DNS、连接、代理、TLS 等。
    NETWORK_ERROR = "network_error"
    # 端点有响应但不是可用的模型响应：返回 HTML / 非 JSON / 流里没有生成 chunk 等。
    INVALID_RESPONSE = "invalid_response"
    # 上游服务端错误（HTTP 5xx）。
    UPSTREAM_ERROR = "upstream_error"
    # 未归类的其他失败。
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelProbeOutcome:
    """一次探活的结果。

    属性:
        ok: 是否产出过至少一个生成 chunk。
        elapsed_ms: 从进入探活到得出结论的毫秒数。
        failure_kind: 失败分类；``ok`` 为 True 时为 None。
        detail: 供日志使用的简短原因（仅异常类型名与 HTTP 状态码，**不含** Key、URL 查询串
            或原始响应正文）；``ok`` 为 True 时为空串。
    """

    ok: bool
    elapsed_ms: int
    failure_kind: ModelProbeFailureKind | None = None
    detail: str = ""


# HTTP 状态码 → 失败分类；未命中的 5xx 归 UPSTREAM_ERROR，其余归 UNKNOWN。
_STATUS_KINDS: dict[int, ModelProbeFailureKind] = {
    400: ModelProbeFailureKind.REQUEST_REJECTED,
    401: ModelProbeFailureKind.AUTH_FAILED,
    403: ModelProbeFailureKind.AUTH_FAILED,
    404: ModelProbeFailureKind.ENDPOINT_NOT_FOUND,
    405: ModelProbeFailureKind.ENDPOINT_NOT_FOUND,
    422: ModelProbeFailureKind.REQUEST_REJECTED,
    429: ModelProbeFailureKind.RATE_LIMITED,
}


async def probe_chat_model(
    model_settings: ModelSettings,
    *,
    max_tokens: int = 1,
    timeout_seconds: float = Constant.LLM.REQUEST_TIMEOUT_SECONDS,
) -> ModelProbeOutcome:
    """用运行期构建入口对一份模型配置做一次真实流式探活。

    参数:
        model_settings: 已物化的模型运行设置（``ModelSettings.from_model_config_record``
            的产物）；未物化时归类为 ``invalid_config``，不发出请求。
        max_tokens: 本次探测的最大生成 token 数；默认 1，读首个 chunk 即结束。
        timeout_seconds: 模型请求阶段（构建之后的流式读取）的超时上限。模型构建是本地同步构造、
            不含网络 I/O，且 ``asyncio`` 超时只能中断 ``await`` 点，因此构建不受该值约束。

    返回:
        :class:`ModelProbeOutcome`；``ok`` 为 True 表示该配置能产出生成内容。

    异常:
        无业务异常；仅 ``asyncio.CancelledError`` 向上传播（调用方取消整个请求时）。

    副作用:
        发起一次真实模型请求（产生极小量 token 消耗）；失败时写一条 ``model_probe_failed``
        warning 日志（含 model / base_url / failure_kind / elapsed_ms，不含 Key）；流关闭失败时
        另写一条 ``model_probe_stream_close_failed`` warning 日志（不改变已得出的结论）。
    """

    started = perf_counter()
    try:
        settings = replace(model_settings, max_tokens=max_tokens)
        settings.require_runtime_config()
    except ModelSettingsError as exc:
        return _failure(started, model_settings, ModelProbeFailureKind.INVALID_CONFIG, str(exc))

    try:
        model = build_chat_model(settings)
        async with asyncio.timeout(timeout_seconds):
            stream = model.astream([HumanMessage(content="ping")])
            try:
                async for _chunk in stream:
                    # 首个生成 chunk 即证明端点、模型与流式通道可用；不等待整段生成。
                    return ModelProbeOutcome(ok=True, elapsed_ms=_elapsed_ms(started))
            finally:
                # 提前返回时必须显式关闭流，避免连接与异步生成器悬挂；关闭本身失败不得改变
                # 已得出的结论（成功不能被降级为失败），因此单独兜住并只记日志。
                try:
                    await stream.aclose()
                except Exception as exc:
                    log.warning(
                        "model_probe_stream_close_failed",
                        extra={
                            "msg": "探活流关闭失败",
                            "data": {
                                "model": settings.model_name,
                                "base_url": settings.base_url,
                                "detail": type(exc).__name__,
                            },
                        },
                    )
    except TimeoutError:
        return _failure(started, model_settings, ModelProbeFailureKind.TIMEOUT, "probe timed out")
    except Exception as exc:
        kind, detail = _classify(exc)
        return _failure(started, model_settings, kind, detail)

    # 循环自然结束且没有任何 chunk：端点有响应但没有可解析的生成内容。
    return _failure(
        started,
        model_settings,
        ModelProbeFailureKind.INVALID_RESPONSE,
        "stream produced no chunk",
    )


def _classify(exc: BaseException) -> tuple[ModelProbeFailureKind, str]:
    """把模型调用异常归一为失败分类与日志用原因。

    只依据异常类型与 ``status_code`` 判定，不解析异常文本（文案随供应商变化，不可靠）。
    """

    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        kind = _STATUS_KINDS.get(status)
        if kind is None:
            kind = (
                ModelProbeFailureKind.UPSTREAM_ERROR
                if status >= 500
                else ModelProbeFailureKind.UNKNOWN
            )
        return kind, f"http_status={status}"
    if isinstance(exc, openai.APITimeoutError | httpx.TimeoutException):
        return ModelProbeFailureKind.TIMEOUT, "request timed out"
    if isinstance(exc, openai.APIConnectionError | httpx.TransportError):
        return ModelProbeFailureKind.NETWORK_ERROR, "connection failed"
    if isinstance(
        exc,
        ValueError | UnicodeDecodeError | json.JSONDecodeError | openai.APIResponseValidationError,
    ):
        return ModelProbeFailureKind.INVALID_RESPONSE, type(exc).__name__
    return ModelProbeFailureKind.UNKNOWN, type(exc).__name__


def _elapsed_ms(started: float) -> int:
    """返回自 ``started`` 起的毫秒数（不修改任何状态）。"""

    return int((perf_counter() - started) * 1000)


def _failure(
    started: float,
    model_settings: ModelSettings,
    kind: ModelProbeFailureKind,
    detail: str,
) -> ModelProbeOutcome:
    """记录失败日志并构造失败结果（不含 Key，不记录原始响应正文）。"""

    elapsed_ms = _elapsed_ms(started)
    log.warning(
        "model_probe_failed",
        extra={
            "msg": "模型探活失败",
            "data": {
                "model": model_settings.model_name,
                "base_url": model_settings.base_url,
                "failure_kind": kind.value,
                "detail": detail,
                "elapsed_ms": elapsed_ms,
            },
        },
    )
    return ModelProbeOutcome(ok=False, elapsed_ms=elapsed_ms, failure_kind=kind, detail=detail)

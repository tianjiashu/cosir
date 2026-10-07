"""读取模型异常中的响应字段并识别无响应体的传输错误。

模型端点的业务错误码与状态码并不统一。本模块不推断余额、限流等业务类别，而是从 SDK 异常
保留的 HTTP 响应体中直接提取 ``message``，交给 Run 错误契约展示。连接和超时通常没有响应体，
此时仅按通用异常类型名识别。

本模块不负责日志、用户文案、Run 状态迁移或重试策略。提取失败时返回 ``None``，不影响 workflow
将 Run 收敛为失败状态。
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator, Mapping
from typing import Any

from app.models.enums.error_kind import ErrorKind


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """按当前异常到最内层 cause 的顺序遍历异常链，并避免循环引用。"""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    """将响应体或错误节点解析为映射；JSON 字符串只在可解析为对象时接受。"""

    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        if isinstance(decoded, Mapping):
            return decoded
    return None


def _error_nodes(body: Any) -> Iterator[Mapping[str, Any]]:
    """按错误对象优先的顺序遍历常见 OpenAI-compatible 响应包装结构。"""

    root = _as_mapping(body)
    if root is None:
        return

    pending: deque[Mapping[str, Any]] = deque([root])
    seen: set[int] = set()
    while pending:
        node = pending.popleft()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node

        # 优先读取明确的 error/detail，再兼容部分端点的 data/result 外层包装。
        for key in ("error", "detail", "data", "result"):
            nested = _as_mapping(node.get(key))
            if nested is not None:
                pending.append(nested)


def _response_body(exc: BaseException) -> Any | None:
    """读取第三方异常的响应体数据属性；不读取状态码或调用异常对象的方法。"""

    # OpenAI SDK 与 LangChain OpenAI 异常会在 body 数据属性中保留解析后的 HTTP 响应体。
    # 这是第三方对象的数据属性读取；异常对象不满足该形状时按无响应体处理。
    try:
        return getattr(exc, "body", None)
    except Exception:
        return None


def extract_model_response_message(exc: BaseException) -> str | None:
    """从模型异常的 HTTP 响应体提取原始 message 字段。

    参数:
        exc: 模型调用链路上抛出的异常；可能是 SDK / LangChain 包装后的异常。

    返回:
        响应体中首个非空 ``message`` / ``msg`` 字符串，保留其原始内容，仅去除首尾空白；
        异常链没有可读取的响应体消息时返回 ``None``。

    异常:
        无。响应体属性读取、JSON 解析或异常链不可用时返回 ``None``，不影响失败收尾。

    副作用:
        无。只读取异常数据属性并解析已存在的响应体，不访问网络、数据库或写日志。
    """

    for current in _exception_chain(exc):
        body = _response_body(current)
        for node in _error_nodes(body):
            for field in ("message", "msg"):
                message = node.get(field)
                if isinstance(message, str) and message.strip():
                    return message.strip()
    return None


def classify_model_failure(exc: BaseException) -> str | None:
    """识别没有 HTTP 响应体的通用传输错误，不猜测 provider 业务错误。

    参数:
        exc: 模型调用链路上抛出的异常；异常可能被 SDK 或 LangChain 包装。

    返回:
        超时或连接失败对应的 ``ErrorKind`` 稳定字符串；其余异常返回 ``None``，由 workflow
        使用通用 Run 失败 code。余额、限流等业务失败的原始说明由
        :func:`extract_model_response_message` 单独提取，不由本函数推断。

    异常:
        无。

    副作用:
        无。只检查异常类型名，不访问网络、数据库、HTTP 状态码或异常文本。
    """

    for current in _exception_chain(exc):
        exception_name = type(current).__name__.lower()
        if "timeout" in exception_name:
            return ErrorKind.MODEL_TIMEOUT.value
        if "connect" in exception_name:
            return ErrorKind.MODEL_NETWORK_ERROR.value
    return None


__all__ = ["classify_model_failure", "extract_model_response_message"]

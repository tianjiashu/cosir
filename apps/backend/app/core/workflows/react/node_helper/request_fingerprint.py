"""模型请求前缀指纹的观测日志。

provider 侧不会回报「这一次为什么没命中缓存」，`usage` 里只有 ``cache_read`` / miss 两个数字。
因此在请求发出前记录可跨 Run、跨进程对比的指纹，把「前缀是否变了」拆成可定位的两级证据：

- ``model_request_fingerprint``（每个模型步一条）：``messages_digest`` 为整段 messages 的摘要，
  两次请求一致即前缀整体一致；``message_digests`` 为逐条消息摘要的序列，整体不一致时按位对比
  即可定位第一处分叉的消息；
- ``model_request_tools_fingerprint``（每个 Run 一条）：``tools_digest`` 为工具 schema 摘要，
  用于把「工具定义变了」与「对话历史变了」区分开。它按 Run 记录而非按步，因为同一 Run 内
  ``bind_tools`` 的 schema 是常量。

签名只纳入会进入 provider 请求的字段（role / content / name / tool_calls 身份与参数 /
``tool_call_id``），刻意不纳入 ``id``、``response_metadata`` 等不上行字段——否则会出现
「实际请求相同、指纹不同」的假阳性，反而误导排查方向。

本模块只读取请求载荷并写日志：不修改消息、不访问数据库、不改变请求内容。它是观测旁路，
任何计算与记录失败都不得影响模型调用及其结果。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.config.logging.logger import log


def _digest(text: str) -> str:
    """对一段文本取短摘要。

    参数:
        text: 待摘要文本。

    返回:
        sha256 十六进制的前 12 位；同一输入恒得同一结果，长度足以在单次对比中区分不同前缀。
    """

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _message_signature(message: BaseMessage) -> str:
    """计算单条消息的请求侧签名。

    只纳入上行字段，与 provider 适配层实际发送的内容对齐（见模块 docstring 的取舍说明）。
    非 ``str`` 的 ``content``（多模态块列表）与 ``tool_calls`` 的参数都经 ``sort_keys`` 归一，
    避免字典顺序差异制造假阳性。

    参数:
        message: 本次请求中的一条 LangChain 消息。

    返回:
        该消息的短摘要。
    """

    parts: list[str] = [message.type]
    if message.name:
        parts.append(f"name={message.name}")
    content = message.content
    if isinstance(content, str):
        parts.append(content)
    else:
        parts.append(json.dumps(content, ensure_ascii=False, sort_keys=True, default=str))
    if isinstance(message, AIMessage):
        for call in message.tool_calls or []:
            parts.append(
                "call|"
                + str(call.get("id"))
                + "|"
                + str(call.get("name"))
                + "|"
                + json.dumps(call.get("args"), ensure_ascii=False, sort_keys=True, default=str)
            )
    if isinstance(message, ToolMessage):
        # tool_call_id 决定 provider 侧调用配对，属上行事实；缺失会漏报「结果错位」类前缀变化。
        parts.append("tool_call_id|" + (message.tool_call_id or ""))
    return _digest("\x1f".join(parts))


def log_messages_fingerprint(
    messages: Sequence[BaseMessage],
    *,
    task_id: int,
    run_id: int,
    step_id: str,
    step_count: int,
) -> None:
    """记录一步模型请求的 messages 前缀指纹。

    产出 event ``model_request_fingerprint``。排查缓存复用问题时，对比两次请求该事件的
    ``message_digests`` 数组：首个不同的下标即前缀分叉的消息位置。

    参数:
        messages: 本步实际发给模型的消息序列（须为 ``resolve_messages_for_model`` 之后的载荷）。
        task_id / run_id / step_id / step_count: 事件定位字段。

    返回:
        无。

    异常:
        无。观测旁路失败（如载荷含不可序列化对象）只记 warning ``model_request_fingerprint_failed``
        后返回，不向上传播，以免影响模型调用。
    """

    try:
        signatures = [_message_signature(message) for message in messages]
    except Exception as exc:  # 观测旁路，失败不得影响模型调用
        log.warning(
            "model_request_fingerprint_failed",
            extra={
                "msg": "模型请求 messages 指纹计算失败，已跳过记录",
                "data": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "step_id": step_id,
                    "error": str(exc),
                },
            },
        )
        return
    log.info(
        "model_request_fingerprint",
        extra={
            "msg": f"模型请求 messages 前缀指纹已记录，step_id={step_id}",
            "data": {
                "task_id": task_id,
                "run_id": run_id,
                "step_id": step_id,
                "step_count": step_count,
                "message_count": len(signatures),
                "messages_digest": _digest("\x1e".join(signatures)),
                "message_digests": signatures,
            },
        },
    )


def log_tools_fingerprint(
    tool_schemas: Sequence[Mapping[str, Any]],
    *,
    task_id: int,
    run_id: int,
) -> None:
    """记录本 Run 绑定到模型的工具 schema 指纹。

    产出 event ``model_request_tools_fingerprint``。同一 Task 内该指纹应当恒定：一旦跨 Run 变化，
    说明工具定义发生了变化（前缀缓存会因此从工具段起失效），可据此把「工具变了」从「历史变了」
    中排除。

    参数:
        tool_schemas: 本 Run 下发给 ``bind_tools`` 的 schema（Task 固化工具目录）。
        task_id / run_id: 事件定位字段。

    返回:
        无。

    异常:
        无。序列化失败只记 warning ``model_request_tools_fingerprint_failed`` 后返回。
    """

    try:
        digest = _digest(
            json.dumps(list(tool_schemas), ensure_ascii=False, sort_keys=True, default=str)
        )
    except Exception as exc:  # 观测旁路，失败不得影响模型调用
        log.warning(
            "model_request_tools_fingerprint_failed",
            extra={
                "msg": "工具 schema 指纹计算失败，已跳过记录",
                "data": {"task_id": task_id, "run_id": run_id, "error": str(exc)},
            },
        )
        return
    log.info(
        "model_request_tools_fingerprint",
        extra={
            "msg": "本 Run 的模型工具 schema 指纹已记录",
            "data": {
                "task_id": task_id,
                "run_id": run_id,
                "tools_count": len(tool_schemas),
                "tools_digest": digest,
            },
        },
    )


__all__ = ["log_messages_fingerprint", "log_tools_fingerprint"]

"""运行时使用的与模型无关的消息协议。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.utils.token_estimator import TokenEstimator


@dataclass(frozen=True)
class RuntimeMessage:
    """表示一个与模型无关的运行时消息。

    参数:
        role: 消息角色，例如 ``system``、``user``、``assistant`` 或 ``tool``。
        content_text: 纯文本消息内容。
        metadata: 用于追踪和后续扩展的可选结构化元数据。

    返回:
        一个运行时消息值对象。

    异常:
        无。

    副作用:
        无。
    """

    role: str
    content_text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # 多模态内容 block（运行期内存，不落库）。user 消息可携带 image_url 等 block，
    # 由 workflow 在运行期经 vision_content_blocks 构造，仅存在于内存态消息，store 落库时忽略。
    content_blocks: list[dict] | None = None

    def estimate_tokens(self) -> int:
        """估算本条消息进入模型上下文后的 token 数（字符启发式，零依赖）。

        计算口径与 ``runtime_context_manager`` 的消息→模型转换严格对齐（只统计真正
        进入模型上下文的字段）：
        - 所有角色的 ``content_text`` 都作为 ``content`` 进入模型，故全部计入；
        - user 消息的 ``content_blocks`` 中 image_url 类 block 经 ``TokenEstimator.estimate_image``
          按厂商上限估算（与编码层同源，避免两处各算一遍导致圆环失真）；
        - ``assistant`` 的 ``metadata["tool_calls"]``（JSON 字符串）会反序列化回
          ``AIMessage.tool_calls``（工具名 + 参数 JSON）进入下一轮 prompt，需额外计入；
        - ``tool`` 的 ``metadata["tool_call_id"]`` 进入 ``ToolMessage.tool_call_id``，需计入；
        - ``system`` / ``user`` 的 ``metadata`` 不进入模型上下文，不计入。
        ``content_text`` 与各字段均经 ``TokenEstimator.estimate``（中英文按字符类别加权）。

        参数:
            无。

        返回:
            本条消息的估算 token 数（空 content 且无工具调用时返回 0）。

        异常:
            无（``metadata["tool_calls"]`` 非合法 JSON 或非预期结构时按 0 计入，不抛）。

        副作用:
            无（纯计算）。
        """
        total = TokenEstimator.estimate(self.content_text)
        if self.content_blocks:
            for block in self.content_blocks:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    total += TokenEstimator.estimate_image(block)
        if self.role == "assistant":
            total += self._estimate_tool_calls()
        elif self.role == "tool":
            total += TokenEstimator.estimate(self.metadata.get("tool_call_id", ""))
        return total

    def _estimate_tool_calls(self) -> int:
        """估算 assistant 消息 ``metadata["tool_calls"]`` 的 token 占用。

        ``tool_calls`` 以 JSON 字符串存储（见 ``runtime_context_manager`` 落库侧序列化），
        反序列化后统计每条调用的工具名与参数 JSON；结构非法或缺失时按 0 计入，不抛。

        参数:
            无。

        返回:
            全部工具调用（工具名 + 参数）的估算 token 数；无效数据返回 0。

        异常:
            无（``json.loads`` 失败或结构不符时容错返回 0）。

        副作用:
            无（纯计算）。
        """
        raw = self.metadata.get("tool_calls")
        if not raw:
            return 0
        try:
            tool_calls = json.loads(raw)
        except (TypeError, ValueError):
            return 0
        if not isinstance(tool_calls, list):
            return 0
        total = 0
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            total += TokenEstimator.estimate(str(call.get("name", "")))
            args = call.get("args")
            if isinstance(args, dict):
                total += TokenEstimator.estimate(json.dumps(args, ensure_ascii=False))
        return total

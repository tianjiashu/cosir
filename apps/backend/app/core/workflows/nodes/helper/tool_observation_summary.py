"""工具观察的 checkpoint 摘要：把一批 ``ToolObservation`` 压成可落 checkpoint 的纯 dict。

``tools`` 节点执行完工具批次后，把本批观察压缩为摘要写入 graph state（``last_tool_results``），
供 ``observe`` 节点判定消费。graph state 全部字段经 checkpointer 持久化，因此摘要必须：

- 只含原生类型（dict / str / bool / int），可直接序列化；
- **不承载** ``data`` 等大体积结构化展示字段（前端展示数据经事件流传递，不进 checkpoint）；
- ``content`` / ``error`` / ``reason`` 经终端输出脱敏后截断到
  ``Settings.TOOL_OBSERVATION_CONTEXT_LIMIT``，避免明文凭据落盘、避免撑爆 checkpoint。

职责边界：
- 负责：观察 → 可序列化摘要的纯转换（脱敏 + 截断 + 丢 ``data``）。
- 不负责：事件分发、模型上下文写回（``tool_observation_dispatcher``）、执行层双通道
  输出预算（``ToolObservationBudget``）。
"""

from typing import Any

from app.config.settings import Settings
from app.core.tools.schemas import ToolObservation
from app.utils.trace_infra.redaction import redact_terminal_output

# 截断后追加的提示尾巴：让 observe 节点与后续 LLM 观察能明确知道内容被截断。
_TRUNCATION_SUFFIX = "\n... [truncated for checkpoint summary]"


def _clamp_text(value: str) -> str:
    """脱敏并按 checkpoint 摘要预算截断一段观察文本。

    先经 ``redact_terminal_output`` 脱敏（复用 utils.trace_infra 既有实现，不自写
    凭据掩码逻辑），再截断到 ``Settings.TOOL_OBSERVATION_CONTEXT_LIMIT``；截断时
    追加明确尾巴，保证消费方知道内容不完整。

    参数:
        value: 待处理文本；空串原样返回。

    返回:
        已脱敏、长度不超过 ``TOOL_OBSERVATION_CONTEXT_LIMIT`` 的文本。

    异常:
        无（纯函数）。

    副作用:
        无。
    """

    if not value:
        return ""
    limit = Settings.TOOL_OBSERVATION_CONTEXT_LIMIT
    redacted = redact_terminal_output(value)
    if len(redacted) <= limit:
        return redacted
    # 预算过小（如测试注入极小值）时尾巴会反超预算：退化为硬截断，仍保证不超限。
    if len(_TRUNCATION_SUFFIX) >= limit:
        return redacted[:limit]
    keep = limit - len(_TRUNCATION_SUFFIX)
    return redacted[:keep] + _TRUNCATION_SUFFIX


def build_tool_result_summaries(
    observations: list[ToolObservation],
    instruction: str | None = None,
) -> dict[str, Any]:
    """把一批工具观察压缩为可序列化摘要，供 ``observe`` 节点判定与后续 LLM 观察使用。

    每条摘要固定携带 ``call_id`` / ``tool_name`` / ``status`` / ``error`` / ``reason``
    / ``content``（脱敏后截断）/ ``retryable`` / ``instruction``（模型意图，无则空串）；
    顺序与 ``observations`` 一致。刻意**不承载** ``data``：展示通道数据由事件流传递，
    落 checkpoint 只保留 observe 判定所需的最小事实。

    参数:
        observations: 本批次工具观察列表（成功/失败/取消均含）。
        instruction: 可选，模型调用工具前的说明文本（来自 ``model`` 节点写入
            ``pending_tool_calls`` 的 ``instruction`` 键），使 ``observe`` 节点在
            错误上限等分支能看到模型当时的意图。缺省视为空串。

    返回:
        ``{"instruction": str, "observations": list[dict]}``；可直接落入 graph state
        与 checkpoint。

    异常:
        无（观察字段均为字符串/布尔，转换纯函数无失败路径）。

    副作用:
        无（纯转换，不修改入参观察对象）。
    """

    summaries: list[dict[str, Any]] = []
    for observation in observations:
        summaries.append(
            {
                "call_id": observation.tool_call_id,
                "tool_name": observation.tool_name,
                "status": observation.status,
                "error": _clamp_text(observation.error),
                "reason": _clamp_text(observation.reason),
                "content": _clamp_text(observation.content),
                "retryable": observation.retryable,
            }
        )
    return {"instruction": instruction or "", "observations": summaries}

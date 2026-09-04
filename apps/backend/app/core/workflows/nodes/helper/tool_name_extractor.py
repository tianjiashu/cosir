"""从模型流式 chunk 提前抽取完整 tool_call 的纯函数。

单一职责：给定一个 LangChain ``AIMessageChunk``，从中尽快（提前）拿到模型本次
意图调用的「全部」完整工具调用。流式场景下工具调用随 ``tool_call_chunks`` 分片
到达，且单 chunk 可能并行携带多个工具调用（各自带 ``index`` 区分），本模块只做
「读 chunk、取调用」的纯转换，不写日志、不落状态、不触达运行时上下文，便于在
model 节点消费 chunk 时尽早路由 / 判断（例如在完整 ``tool_calls`` 组装前提前
感知调用的工具集合）。

返回的每项是 ``tool_call_chunks`` 原生条目（含 ``name`` / ``args`` / ``id`` /
``index`` 的字典），是「当前 chunk 可见」的最完整工具调用形态。

与 ``chunk_assembler``（chunk 合并成 ``AIMessage``、跨分片拼接 args）、
``invalid_tool_call``（非法调用处置）职责分离：本模块只关心「这个 chunk 现在能
告诉我哪些完整工具调用」，不负责跨 chunk 合并被切片的 args。无循环导入：不
import ``model_node`` / ``common``。
"""

from typing import Any

from langchain_core.messages import AIMessageChunk


def _collect_tool_calls(target: object) -> list[dict[str, Any]]:
    """从 ``tool_call_chunks`` 或 ``tool_calls`` 条目中收集工具调用字典。

    参数:
        target: ``tool_call_chunks``（``ToolCallChunk`` 字典列表）或 ``tool_calls``
            （``{"name": ..., "args": ..., "id": ..., "index": ...}`` 字典列表）；
            非 list 时安全返回空列表。

    返回:
        原始工具调用字典列表（保留 ``name`` / ``args`` / ``id`` / ``index`` 字段，
        不裁剪、不跨条目合并）；无可识别工具调用时返回空列表 ``[]``。

    副作用:
        无（仅读取，不改 ``target``）。
    """
    if not isinstance(target, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in target:
        if isinstance(item, dict):
            calls.append(dict(item))
    return calls


def extract_tool_calls(chunk: AIMessageChunk) -> list[dict[str, Any]]:
    """从 AIMessageChunk 提前抽取全部完整 tool_call（保留原生字段）。

    优先读 ``tool_call_chunks``（流式分片的主要载体，可能并行含多个工具调用，
    每项自带 ``name`` / ``args`` / ``id`` / ``index``），再回退读 ``tool_calls``
    （例如已聚合的末 chunk、``chunk_position == "last"`` 时工具调用已被解析进
    ``tool_calls``）。任一口非空即返回该口全部条目，不做跨口去重或合并。

    注意：流式分片中 ``args`` 通常是未闭合的部分 JSON（跨 chunk 拼接属于
    ``chunk_assembler`` 职责），本函数只返回「当前 chunk 可见」的完整条目，不跨
    chunk 拼接。需要最终完整工具调用请在工具节点合并后从 ``tool_calls`` 读取。

    参数:
        chunk: 模型 ``astream`` 产出的 LangChain ``AIMessageChunk``（不要求已聚合）。

    返回:
        当前 chunk 可见的全部完整工具调用（``list[dict[str, Any]]``，每项含
        ``name`` / ``args`` / ``id`` / ``index``）；无可识别工具调用时返回空列表
        ``[]``（而非 ``None``，便于调用方直接迭代）。

    异常:
        无（对缺失属性、非预期结构、空字段均安全降级为空列表）。

    副作用:
        无（纯读取 chunk 字段，不写任何外部状态）。
    """
    # 1) 优先从流式分片 tool_call_chunks 收集（可能并行多工具调用）。
    tool_call_chunks = getattr(chunk, "tool_call_chunks", None)
    calls = _collect_tool_calls(tool_call_chunks)
    if calls:
        return calls

    # 2) 回退：聚合末 chunk 已把调用解析进 tool_calls。
    return _collect_tool_calls(getattr(chunk, "tool_calls", None))


__all__ = ["extract_tool_calls"]

#!/usr/bin/env python3
"""业务库会话上下文回放查询（log-triage skill 内置）。

单一职责：只读解析 ``conversation_task_contexts`` 的 canonical 消息行，产出两类排障视图——
「逐条消息回放」与「工具调用 / 结果配对汇总」，使 Agent 行为问题（答非所问、工具参数错、
工具失败、上下文缺口）可以在不启动服务的情况下复盘。

职责边界：
- 负责：按 task / run 读取消息行、解析 LangChain 序列化消息（``message_json``）与
  Transport metadata（``transport_metadata_json``）、按 ``tool_call_id`` 配对调用与结果、
  汇总上下文计数。
- 不负责：run 生命周期状态（见 ``appdb_agent_facts``）、文件变更（见 ``appdb_side_effects``）、
  渲染与截断（见 CLI 入口）。

数据结构事实：``message_json`` 为 ``{"type": "ai"|"tool"|"human"|"system", "data": {...}}``；
AI 的工具调用在 ``data.tool_calls[]``（``{id, name, args}``）；工具结果在
``data.content`` + ``data.tool_call_id`` + ``data.status``；Transport 侧的工具生命周期状态
在 ``transport_metadata_json.status``（pending/running/completed/failed/cancelled）。
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from typing import Any

from appdb_readonly import query_rows, require_tables, safe_json
from sqlite_values import as_bool, bool_true_sql

_TOOL_ERROR_PREFIX = "error:"


def list_messages(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    run_id: int | None = None,
    limit: int,
    order: str = "asc",
    include_streaming: bool = True,
) -> list[dict[str, Any]]:
    """按插入顺序回放某任务（或某次运行）的会话上下文消息。

    参数:
        connection: 只读 SQLite 连接。
        task_id: 任务标识。
        run_id: 可选运行过滤；为 None 时返回该任务全部运行的消息。
        limit: 最大返回行数。
        order: ``asc``（按 sequence 升序回放，默认）或 ``desc``（最近的在前）。
        include_streaming: 是否包含仅服务 Transport 冷重建的流式草稿（``is_streaming=1``）。

    返回:
        消息行列表，每行含 ``sequence`` / ``run_id`` / ``kind`` / ``tool_call_id`` /
        ``is_streaming`` / ``include_in_context`` / ``transport_status`` / ``text`` /
        ``tool_calls`` / ``usage`` / ``error``。

    异常:
        ValueError: ``conversation_task_contexts`` 表缺失，或 ``order`` 非法。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "conversation_task_contexts")
    if order not in ("asc", "desc"):
        raise ValueError("order must be asc or desc")
    where = ["task_id = ?"]
    params: list[Any] = [task_id]
    if run_id is not None:
        where.append("run_id = ?")
        params.append(run_id)
    if not include_streaming:
        # 走与 as_bool 同源的 SQL 判定，避免裸比较漏掉 TEXT 形态的布尔值。
        where.append(f"NOT ({bool_true_sql('is_streaming')})")
    direction = "ASC" if order == "asc" else "DESC"
    sql = (
        "SELECT id, task_id, run_id, tool_call_id, sequence, is_streaming, "
        "include_in_context, transport_metadata_json, message_json "
        "FROM conversation_task_contexts WHERE "
        + " AND ".join(where)
        + f" ORDER BY sequence {direction} LIMIT ?"
    )
    params.append(limit)
    rows = query_rows(connection, sql, tuple(params))
    return [_describe_message(row) for row in rows]


def summarize_tool_calls(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    run_id: int | None = None,
    contains: str = "",
    failures_only: bool = False,
    limit: int,
) -> list[dict[str, Any]]:
    """按 ``tool_call_id`` 配对工具调用与结果，汇总每次工具调用的结局。

    配对在**单遍、按 ``sequence`` 顺序**上进行，未配对调用按 ``(run_id, tool_call_id)`` 维护 FIFO
    队列、结果行只认领同一 run 内最早的未配对调用，因此并发调用、跨 run 复用同一 id、取消占位都能
    正确还原（若只按 ``tool_call_id`` 配对，「run1 的调用无结果 + run2 复用同一 id 且有结果」会把
    run2 的结果错挂到 run1）。

    待定状态的判读口径（重要）：
    - ``cancelled``：run 被取消或后端重启恢复时，``ConversationTaskContextService
      .close_unclosed_tool_calls_for_run``（连同 ``ConversationRunService.recover_orphaned_runs``）
      会为未闭合调用**补一行占位 ToolMessage** 并把 Transport 状态记为 ``cancelled``，因此这类
      调用通常表现为 ``cancelled`` 而非缺结果。
    - ``pending``：仅当占位补行**未执行**（例如进程被强杀且之后未再启动）时才出现。
    - 缺少调用行的孤立结果不产出条目；未携带 ``id`` 的调用按协议无需配对，同样不产出条目。

    参数:
        connection: 只读 SQLite 连接。
        task_id: 任务标识。
        run_id: 可选运行过滤。
        contains: 可选关键字，匹配工具名 / 参数字典 / 结果文本。
        failures_only: 是否只返回失败（``status == "error"`` 或结果以 ``error:`` 开头）的调用。
            **注意**：``pending`` / ``cancelled`` 条目不算失败，会被本开关过滤掉。
        limit: 最大返回条数。**limit 作用于产出条目数**，SQL 不做 LIMIT（需先全量扫描该
            task 的消息行才能完成配对），大 task 下存在全量扫描开销。

    返回:
        工具调用汇总列表，每项含 ``tool_call_id`` / ``tool_name`` / ``status`` /
        ``is_error`` / ``args`` / ``result_summary`` / ``request_sequence`` /
        ``result_sequence``；按请求顺序排列。

    异常:
        ValueError: ``conversation_task_contexts`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "conversation_task_contexts")
    where = ["task_id = ?"]
    params: list[Any] = [task_id]
    if run_id is not None:
        where.append("run_id = ?")
        params.append(run_id)
    sql = (
        "SELECT id, run_id, tool_call_id, sequence, transport_metadata_json, message_json "
        "FROM conversation_task_contexts WHERE " + " AND ".join(where) + " ORDER BY sequence ASC"
    )
    rows = query_rows(connection, sql, tuple(params))

    # 单遍、按 sequence 顺序配对：未配对调用按 (run_id, tool_call_id) 建 FIFO 队列，结果行只
    # 认领**同一 run 内**最早的未配对调用。为何必须带 run_id 而不是只用 tool_call_id——同一 task
    # 内 tool_call_id 可跨 run 复用（唯一约束是 (task_id, run_id, tool_call_id)），若只按 id 配对，
    # 「run1 的调用没有结果、run2 复用同一 id 且有结果」时会把 run2 的结果错挂到 run1。
    calls: list[dict[str, Any]] = []
    unmatched: dict[tuple[Any, str], deque[dict[str, Any]]] = {}
    for row in rows:
        message = safe_json(row.get("message_json"), default={})
        if not isinstance(message, dict):
            continue
        data = message.get("data")
        if not isinstance(data, dict):
            continue
        kind = message.get("type")
        if kind == "ai":
            for call in data.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                if not call_id:
                    # 协议上未携带 id 的调用无需配对（与后端 ``tool_call_closure`` 的 plan 一致），
                    # 不产出条目，避免把实际已执行的调用渲染成永久的 pending。
                    continue
                entry = {
                    "tool_call_id": call_id,
                    "tool_name": call.get("name") or "<unknown>",
                    "args": call.get("args"),
                    "status": "pending",
                    "is_error": False,
                    "result_summary": "",
                    "run_id": row.get("run_id"),
                    "request_sequence": row.get("sequence"),
                    "result_sequence": None,
                }
                calls.append(entry)
                unmatched.setdefault((row.get("run_id"), call_id), deque()).append(entry)
            continue
        if kind != "tool":
            continue
        call_id = str(row.get("tool_call_id") or data.get("tool_call_id") or "")
        entry = _claim_unmatched_call(unmatched, row.get("run_id"), call_id)
        if entry is None:
            # 缺调用行的孤立结果（如上下文被压缩掉 AI 消息）不产出条目，避免伪造调用。
            continue
        content = data.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        reported = str(data.get("status") or "")
        transport_status = _transport_status(row.get("transport_metadata_json"))
        entry["status"] = reported or transport_status or "completed"
        entry["is_error"] = reported == "error" or text.lstrip().startswith(_TOOL_ERROR_PREFIX)
        entry["result_summary"] = text
        entry["result_sequence"] = row.get("sequence")

    selected = [call for call in calls if _matches(call, contains)]
    if failures_only:
        selected = [call for call in selected if call["is_error"]]
    return selected[:limit]


def _claim_unmatched_call(
    unmatched: dict[tuple[Any, str], deque[dict[str, Any]]],
    run_id: Any,
    call_id: str,
) -> dict[str, Any] | None:
    """认领一条未配对调用。

    优先在 ``(run_id, call_id)`` 精确键内按 FIFO 认领队首（正确覆盖「同一 run 内出现重复 id」的
    畸形数据）；精确键缺失时再宽松回退到**任意** run 下同 ``call_id`` 的队首，以容忍结果行 run_id
    与调用行不一致的异常数据。两者都无匹配时返回 None（孤儿结果）。

    参数:
        unmatched: 未配对调用队列索引，键为 ``(run_id, tool_call_id)``。
        run_id: 结果行所属 run。
        call_id: 结果行引用的工具调用 id。

    返回:
        被认领的调用条目字典；无匹配时返回 None。

    异常:
        无。

    副作用:
        命中时从对应队列弹出该条目（就地修改 ``unmatched``）。
    """

    queue = unmatched.get((run_id, call_id))
    if queue:
        return queue.popleft()
    for (_, candidate_id), candidate_queue in unmatched.items():
        if candidate_id == call_id and candidate_queue:
            return candidate_queue.popleft()
    return None


def context_statistics(connection: sqlite3.Connection, *, task_id: int) -> dict[str, Any]:
    """汇总某任务的上下文规模与构成。

    参数:
        connection: 只读 SQLite 连接。
        task_id: 任务标识。

    返回:
        ``{"total", "in_context", "streaming_drafts", "runs", "by_kind"}``：
        消息总数、纳入模型上下文条数、流式草稿条数、涉及运行数，以及按消息类型分组的条数。

    异常:
        ValueError: ``conversation_task_contexts`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "conversation_task_contexts")
    rows = query_rows(
        connection,
        "SELECT run_id, is_streaming, include_in_context, message_json "
        "FROM conversation_task_contexts WHERE task_id = ?",
        (task_id,),
    )
    by_kind: dict[str, int] = {}
    runs: set[Any] = set()
    in_context = 0
    drafts = 0
    for row in rows:
        message = safe_json(row.get("message_json"), default={})
        kind = message.get("type") if isinstance(message, dict) else None
        by_kind[str(kind or "unknown")] = by_kind.get(str(kind or "unknown"), 0) + 1
        if as_bool(row.get("include_in_context")):
            in_context += 1
        if as_bool(row.get("is_streaming")):
            drafts += 1
        if row.get("run_id") is not None:
            runs.add(row.get("run_id"))
    return {
        "total": len(rows),
        "in_context": in_context,
        "streaming_drafts": drafts,
        "runs": len(runs),
        "by_kind": by_kind,
    }


def _describe_message(row: dict[str, Any]) -> dict[str, Any]:
    """把一条上下文行规约为回放视图。

    参数:
        row: ``conversation_task_contexts`` 查询行。

    返回:
        含消息类型、文本、工具调用、用量与 Transport 状态的字典。

    异常:
        无。

    副作用:
        无。
    """

    message = safe_json(row.get("message_json"), default={})
    data = message.get("data") if isinstance(message, dict) else None
    if not isinstance(data, dict):
        data = {}
    tool_calls = []
    for call in data.get("tool_calls") or []:
        if isinstance(call, dict):
            tool_calls.append(
                {"id": call.get("id"), "name": call.get("name"), "args": call.get("args")}
            )
    return {
        "sequence": row.get("sequence"),
        "run_id": row.get("run_id"),
        "kind": message.get("type") if isinstance(message, dict) else None,
        "tool_call_id": row.get("tool_call_id") or data.get("tool_call_id"),
        "is_streaming": as_bool(row.get("is_streaming")),
        "include_in_context": as_bool(row.get("include_in_context")),
        "transport_status": _transport_status(row.get("transport_metadata_json")),
        "result_status": data.get("status"),
        "text": _message_text(data),
        "tool_calls": tool_calls,
        "usage": data.get("usage_metadata"),
        "error": _transport_error(row.get("transport_metadata_json")),
    }


def _message_text(data: dict[str, Any]) -> str:
    """提取消息正文文本。

    参数:
        data: 序列化消息的 ``data`` 字段。

    返回:
        ``content`` 为字符串时原样返回；为多模态列表时取其中文本片段拼接；
        无文本时返回空字符串。

    异常:
        无。

    副作用:
        无。
    """

    content = data.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(parts)
    return ""


def _transport_status(raw: Any) -> str:
    """读取 Transport metadata 中的工具生命周期状态。

    参数:
        raw: ``transport_metadata_json`` 原文。

    返回:
        状态字符串；缺失或无法解析时返回空字符串。

    异常:
        无。

    副作用:
        无。
    """

    metadata = safe_json(raw if isinstance(raw, str) else None, default={})
    if isinstance(metadata, dict) and isinstance(metadata.get("status"), str):
        return metadata["status"]
    return ""


def _transport_error(raw: Any) -> str:
    """读取 Transport metadata 中的错误文本。

    参数:
        raw: ``transport_metadata_json`` 原文。

    返回:
        错误文本；缺失时为 ``str(metadata["error"])`` 的空值形态（空字符串）。

    异常:
        无。

    副作用:
        无。
    """

    metadata = safe_json(raw if isinstance(raw, str) else None, default={})
    if isinstance(metadata, dict) and metadata.get("error"):
        return str(metadata["error"])
    return ""


def _matches(call: dict[str, Any], contains: str) -> bool:
    """判断一次工具调用是否命中关键字过滤。

    参数:
        call: 工具调用汇总项。
        contains: 关键字；空字符串表示不过滤。

    返回:
        命中返回 True。

    异常:
        无。

    副作用:
        无。
    """

    if not contains:
        return True
    haystack = json.dumps(
        {
            "tool_name": call.get("tool_name"),
            "args": call.get("args"),
            "result_summary": call.get("result_summary"),
        },
        ensure_ascii=False,
    )
    return contains.lower() in haystack.lower()

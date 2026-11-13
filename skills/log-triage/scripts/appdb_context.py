#!/usr/bin/env python3
"""业务库会话上下文回放查询（log-triage skill 内置）。

单一职责：只读解析 ``conversation_task_contexts`` 的消息行，产出两类排障视图——「逐条消息回放」与
「工具调用 / 结果配对汇总」，使 Agent 行为问题（答非所问、工具参数错、工具失败、上下文缺口）可以在
不启动服务的情况下复盘。

职责边界：
- 负责：按 task / run 读取消息行、解析 ``message_json`` 与 ``metadata_json``、按 ``tool_call_id``
  配对调用与结果、汇总上下文计数。
- 不负责：run 生命周期状态与委派子任务（见 ``appdb_agent_facts``）、渲染与截断（见 CLI 入口）。

数据结构事实（随后端 ``ContextEntry`` 同步）：

- ``message_json`` 是 OpenAI SDK 请求消息：``{"role": "system"|"user"|"assistant"|"tool", ...}``。
  assistant 的工具调用在 ``tool_calls[]``（``{id, type, function: {name, arguments}}``，
  ``arguments`` 是 JSON 字符串）；tool 消息带 ``tool_call_id`` 与文本 ``content``。
- ``metadata_json`` 按角色是不同结构：user 行带 ``kind``（``user_input`` / ``agent_message`` /
  ``notice`` / ``background_result`` / ``user_decision`` / ``tool_image`` 等）；assistant 行带
  ``model`` / ``finish_reason`` / ``usage``；tool 行带卡片状态 ``status``（pending / running /
  completed / failed / cancelled）、``error`` 与后台工作记录 ``background``。
- ``include_in_context=0`` 的行不进模型上下文：流式中或被中断的回复、撤回的回合都是这样。
- ``tool_call_id`` 在 Task 内唯一（后端冲突时改写厂商 id），所以按 id 配对即可，不必带 run。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from appdb_readonly import query_rows, require_tables, safe_json
from sqlite_values import as_bool, bool_true_sql

_COLUMNS = (
    "id, task_id, run_id, sequence, turn, include_in_context, project_to_transport, "
    "metadata_json, message_json"
)


def list_messages(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    run_id: int | None = None,
    limit: int,
    order: str = "asc",
    in_context_only: bool = False,
) -> list[dict[str, Any]]:
    """按 ``sequence`` 回放某任务（或某次运行）的上下文消息。

    参数:
        connection: 只读 SQLite 连接。
        task_id: 任务标识。
        run_id: 可选运行过滤；为 None 时返回该任务全部运行的消息（含继承来的、``run_id`` 为空的行）。
        limit: 最大返回行数。
        order: ``asc``（升序回放，默认）或 ``desc``（最近的在前）。
        in_context_only: 只返回进模型上下文的行（排除流式 / 中断的回复与撤回的回合）。

    返回:
        消息行列表，每行含 ``sequence`` / ``run_id`` / ``turn`` / ``role`` / ``kind`` /
        ``tool_call_id`` / ``include_in_context`` / ``project_to_transport`` / ``tool_status`` /
        ``text`` / ``tool_calls`` / ``usage`` / ``error``。

    异常:
        ValueError: ``conversation_task_contexts`` 表缺失，或 ``order`` 非法。
        sqlite3.Error: 查询失败。
    """

    require_tables(connection, "conversation_task_contexts")
    if order not in ("asc", "desc"):
        raise ValueError("order must be asc or desc")
    where = ["task_id = ?"]
    params: list[Any] = [task_id]
    if run_id is not None:
        where.append("run_id = ?")
        params.append(run_id)
    if in_context_only:
        # 走与 as_bool 同源的 SQL 判定，避免裸比较漏掉 TEXT 形态的布尔值。
        where.append(bool_true_sql("include_in_context"))
    direction = "ASC" if order == "asc" else "DESC"
    sql = (
        f"SELECT {_COLUMNS} FROM conversation_task_contexts WHERE "  # noqa: S608 - 列名为常量
        + " AND ".join(where)
        + f" ORDER BY sequence {direction} LIMIT ?"
    )
    params.append(limit)
    return [_describe_message(row) for row in query_rows(connection, sql, tuple(params))]


def summarize_tool_calls(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    run_id: int | None = None,
    contains: str = "",
    failures_only: bool = False,
    limit: int,
) -> list[dict[str, Any]]:
    """按 ``tool_call_id`` 配对工具调用与结果，汇总每次调用的结局。

    状态取结果行 metadata 的 ``status``（卡片状态的事实源）。判读口径：

    - ``pending`` 且带 ``background``：后台命令还在跑（或重启前没结算完）；
    - ``pending`` 不带 ``background``：等用户决定（HITL）或交接等交还；
    - ``running``：决定已接收、正在重放执行；
    - 没有结果行（``no_result``）：调用后进程被强杀且没再启动，或 assistant 行未进上下文。
    Run 被取消或重启收敛时，后端会把残留占位推到 ``cancelled``，所以正常库里 ``no_result`` 很少见。

    参数:
        connection: 只读 SQLite 连接。
        task_id: 任务标识。
        run_id: 可选运行过滤（按发起调用的 assistant 行所属 run）。
        contains: 可选关键字，匹配工具名 / 参数 / 结果文本。
        failures_only: 只返回 ``failed`` 的调用。
        limit: 最大返回条数；作用于产出条目，SQL 需要全量扫描该 task 的消息行才能配对。

    返回:
        工具调用汇总列表，按请求顺序；每项含 ``tool_call_id`` / ``tool_name`` / ``status`` /
        ``background`` / ``error`` / ``args`` / ``result_summary`` / ``run_id`` / ``turn`` /
        ``request_sequence`` / ``result_sequence``。

    异常:
        ValueError: ``conversation_task_contexts`` 表缺失。
        sqlite3.Error: 查询失败。
    """

    require_tables(connection, "conversation_task_contexts")
    rows = query_rows(
        connection,
        f"SELECT {_COLUMNS} FROM conversation_task_contexts "  # noqa: S608 - 列名为常量
        "WHERE task_id = ? ORDER BY sequence ASC",
        (task_id,),
    )
    calls: dict[str, dict[str, Any]] = {}
    for row in rows:
        message = _json_dict(row.get("message_json"))
        role = message.get("role")
        if role == "assistant":
            if run_id is not None and row.get("run_id") != run_id:
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict) or not call.get("id"):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                calls[str(call["id"])] = {
                    "tool_call_id": str(call["id"]),
                    "tool_name": function.get("name") or "<unknown>",
                    "args": _parse_arguments(function.get("arguments")),
                    "status": "no_result",
                    "background": None,
                    "error": "",
                    "result_summary": "",
                    "run_id": row.get("run_id"),
                    "turn": row.get("turn"),
                    "request_sequence": row.get("sequence"),
                    "result_sequence": None,
                }
        elif role == "tool":
            entry = calls.get(str(message.get("tool_call_id") or ""))
            if entry is None:
                # 调用不在筛选范围内，或缺调用行的孤立结果：不伪造调用。
                continue
            metadata = _json_dict(row.get("metadata_json"))
            entry["status"] = str(metadata.get("status") or "completed")
            entry["background"] = metadata.get("background")
            entry["error"] = str(metadata.get("error") or "")
            entry["result_summary"] = _text(message.get("content"))
            entry["result_sequence"] = row.get("sequence")

    selected = [call for call in calls.values() if _matches(call, contains)]
    if failures_only:
        selected = [call for call in selected if call["status"] == "failed"]
    return selected[:limit]


def context_statistics(connection: sqlite3.Connection, *, task_id: int) -> dict[str, Any]:
    """汇总某任务的上下文规模与构成。

    返回:
        ``{"total", "in_context", "excluded", "runs", "turns", "by_role", "by_user_kind"}``：
        消息总数、进模型上下文的条数、不进的条数（流式 / 中断 / 撤回）、涉及的 run 数与回合数、
        按角色分组的条数，以及 user 行按 metadata ``kind`` 分组的条数。

    异常:
        ValueError: ``conversation_task_contexts`` 表缺失。
        sqlite3.Error: 查询失败。
    """

    require_tables(connection, "conversation_task_contexts")
    rows = query_rows(
        connection,
        "SELECT run_id, turn, include_in_context, metadata_json, message_json "
        "FROM conversation_task_contexts WHERE task_id = ?",
        (task_id,),
    )
    by_role: dict[str, int] = {}
    by_user_kind: dict[str, int] = {}
    runs: set[Any] = set()
    turns: set[Any] = set()
    in_context = 0
    for row in rows:
        role = str(_json_dict(row.get("message_json")).get("role") or "unknown")
        by_role[role] = by_role.get(role, 0) + 1
        if role == "user":
            kind = str(_json_dict(row.get("metadata_json")).get("kind") or "unknown")
            by_user_kind[kind] = by_user_kind.get(kind, 0) + 1
        if as_bool(row.get("include_in_context")):
            in_context += 1
        if row.get("run_id") is not None:
            runs.add(row.get("run_id"))
        if row.get("turn"):
            turns.add(row.get("turn"))
    return {
        "total": len(rows),
        "in_context": in_context,
        "excluded": len(rows) - in_context,
        "runs": len(runs),
        "turns": len(turns),
        "by_role": by_role,
        "by_user_kind": by_user_kind,
    }


def _describe_message(row: dict[str, Any]) -> dict[str, Any]:
    """把一条上下文行规约为回放视图。"""

    message = _json_dict(row.get("message_json"))
    metadata = _json_dict(row.get("metadata_json"))
    role = message.get("role")
    tool_calls = []
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            tool_calls.append(
                {
                    "id": call.get("id"),
                    "name": function.get("name"),
                    "args": _parse_arguments(function.get("arguments")),
                }
            )
    return {
        "sequence": row.get("sequence"),
        "run_id": row.get("run_id"),
        "turn": row.get("turn"),
        "role": role,
        # user 行的输入种类；assistant 行给结束原因，便于一眼看出被截断或调用了工具。
        "kind": metadata.get("kind") if role == "user" else metadata.get("finish_reason"),
        "tool_call_id": message.get("tool_call_id"),
        "include_in_context": as_bool(row.get("include_in_context")),
        "project_to_transport": as_bool(row.get("project_to_transport")),
        "tool_status": metadata.get("status") if role == "tool" else None,
        "text": _text(message.get("content")),
        "tool_calls": tool_calls,
        "usage": metadata.get("usage") if role == "assistant" else None,
        "error": str(metadata.get("error") or "") if role == "tool" else "",
    }


def _json_dict(raw: Any) -> dict[str, Any]:
    """解析 JSON 列为字典；缺失、损坏或不是对象时返回空字典。"""

    value = safe_json(raw if isinstance(raw, str) else None, default={})
    return value if isinstance(value, dict) else {}


def _parse_arguments(raw: Any) -> Any:
    """工具参数是 JSON 字符串；能解析就返回对象，否则原样返回（模型可能写出非法 JSON）。"""

    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _text(content: Any) -> str:
    """提取消息正文：字符串原样返回，多段内容只取文本段拼接。"""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _matches(call: dict[str, Any], contains: str) -> bool:
    """判断一次工具调用是否命中关键字过滤；空关键字不过滤。"""

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

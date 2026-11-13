"""log-triage 测试用业务库夹具：表结构直接来自后端 ORM。

为什么不手写 CREATE TABLE：skill 是对后端业务库的只读排查工具，测试库若自带一份 schema，
后端改表后测试照样绿，漂移只能在真实库上才暴露。这里调用后端建表入口
``app.storage.store_engines.initialize_app_schema``，后端一改列，这里的插入函数或 skill 查询就会失败。

插入函数只填当前 ORM 的必填列与测试关心的列，统一用 sqlite3 写入（ORM 只负责建表），
以便用例能故意写入非常规取值（如 TEXT 形态的布尔、损坏的 JSON）。测试需在后端 venv 下运行。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parents[3] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sqlalchemy import create_engine  # noqa: E402

from app.storage.store_engines import initialize_app_schema  # noqa: E402

_TS = "2026-01-01T00:00:00"


def create_app_db(path: Path) -> sqlite3.Connection:
    """在 ``path`` 用后端 schema 建库，返回可写连接（``row_factory`` 为 ``sqlite3.Row``）。"""

    engine = create_engine(f"sqlite:///{path}")
    try:
        initialize_app_schema(engine)
    finally:
        engine.dispose()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
    values = {"created_at": _TS, "updated_at": _TS, **values}
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})",  # noqa: S608 - 测试夹具，列名来自调用方字面量
        tuple(values.values()),
    )
    connection.commit()
    return int(cursor.lastrowid or 0)


def add_workspace(connection: sqlite3.Connection, **values: Any) -> int:
    return _insert(connection, "workspaces", {"name": "ws", "root_path": "/tmp/ws", **values})


def add_task(connection: sqlite3.Connection, *, workspace_id: int = 1, **values: Any) -> int:
    return _insert(
        connection,
        "tasks",
        {"workspace_id": workspace_id, "title": "task", "tool_schemas": "[]", **values},
    )


def add_run(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    status: str = "completed",
    model_config_id: str = "deepseek/deepseek-flash",
    **values: Any,
) -> int:
    return _insert(
        connection,
        "conversation_runs",
        {
            "task_id": task_id,
            "status": status,
            "model_config_id": model_config_id,
            "agent_id": "main",
            "execution_data": "{}",
            "options": "{}",
            **values,
        },
    )


def add_context(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    run_id: int | None,
    sequence: int,
    message: dict[str, Any] | str,
    metadata: dict[str, Any] | None = None,
    turn: int = 1,
    include_in_context: object = 1,
    project_to_transport: object = 1,
) -> int:
    """插入一条上下文行；``message`` 传字符串时原样写入（用于损坏 JSON 用例）。"""

    return _insert(
        connection,
        "conversation_task_contexts",
        {
            "task_id": task_id,
            "run_id": run_id,
            "sequence": sequence,
            "turn": turn,
            "message_json": message if isinstance(message, str) else json.dumps(message, ensure_ascii=False),
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
            "include_in_context": include_in_context,
            "project_to_transport": project_to_transport,
        },
    )


def add_task_message(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    kind: str = "user_input",
    delivery: str = "next_run",
    payload: dict[str, Any] | None = None,
    project_to_transport: bool = True,
) -> int:
    return _insert(
        connection,
        "task_messages",
        {
            "task_id": task_id,
            "kind": kind,
            "delivery": delivery,
            "payload": json.dumps(payload or {}),
            "project_to_transport": int(project_to_transport),
        },
    )


# ---- OpenAI SDK 消息形状 ---------------------------------------------------- #


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def assistant_message(text: str = "", *calls: tuple[str, str, Any]) -> dict[str, Any]:
    """``calls`` 为 ``(id, name, args)``；``args`` 非字符串时序列化为 JSON 字符串。"""

    message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": args if isinstance(args, str) else json.dumps(args),
                },
            }
            for call_id, name, args in calls
        ]
    return message


def tool_message(tool_call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "content": content, "tool_call_id": tool_call_id}

"""Generate shared TypeScript API types from backend Pydantic schemas."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import UnionType
from typing import Any, get_args, get_origin

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "apps" / "backend"
SHARED_TS_ROOT = REPO_ROOT / "apps" / "shared" / "ts"

sys.path.insert(0, str(BACKEND_ROOT))

from app.api.schemas.request.CreateTaskRequest import CreateTaskRequest  # noqa: E402
from app.api.schemas.request.CreateTurnRequest import CreateTurnRequest  # noqa: E402
from app.api.schemas.request.CreateWorkspaceRequest import CreateWorkspaceRequest  # noqa: E402
from app.api.schemas.request.QueryLogsRequest import QueryLogsRequest  # noqa: E402
from app.api.schemas.request.RecentLogsRequest import RecentLogsRequest  # noqa: E402
from app.api.schemas.response.AgentProfileResponse import AgentProfileResponse  # noqa: E402
from app.api.schemas.response.DeleteWorkspaceResponse import DeleteWorkspaceResponse  # noqa: E402
from app.api.schemas.response.HealthResponse import HealthResponse  # noqa: E402
from app.api.schemas.response.ListAgentsResponse import ListAgentsResponse  # noqa: E402
from app.api.schemas.response.LogEntryResponse import LogEntryResponse  # noqa: E402
from app.api.schemas.response.LogQueryResponse import LogQueryResponse  # noqa: E402
from app.api.schemas.response.TaskResponse import TaskResponse  # noqa: E402
from app.api.schemas.response.TurnResponse import TurnResponse  # noqa: E402
from app.api.schemas.response.WorkspaceResponse import WorkspaceResponse  # noqa: E402
from app.models.enums.turn_status import TurnStatus  # noqa: E402


def main() -> None:
    """生成前端共享 HTTP API TypeScript 类型。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        覆盖写入 ``apps/shared/ts`` 下的 API 相关类型文件。
    """

    write("task.ts", render_task_types())
    write("turn.ts", render_turn_types())
    write(
        "workspace.ts",
        render_module(
            [WorkspaceResponse],
            "workspace",
            aliases={"WorkspaceRecord": "WorkspaceResponse"},
        ),
    )
    write(
        "logs.ts",
        render_logs_types(),
    )
    write(
        "agents.ts", render_module([AgentProfileResponse, ListAgentsResponse], "agents")
    )
    write("api.ts", render_api_types())


def write(relative_path: str, content: str) -> None:
    """写入一个生成后的 TS 文件。

    参数:
        relative_path: 相对 ``apps/shared/ts`` 的输出路径。
        content: 文件内容。

    返回:
        无。

    异常:
        OSError: 当文件写入失败时由 pathlib 抛出。

    副作用:
        覆盖目标文件。
    """

    (SHARED_TS_ROOT / relative_path).write_text(content, encoding="utf-8")


def render_task_types() -> str:
    """渲染任务共享类型。

    参数:
        无。

    返回:
        ``task.ts`` 内容。

    异常:
        无。

    副作用:
        无。
    """

    return (
        generated_header("task")
        + "\n"
        + "export type TaskStatus = string;\n\n"
        + render_interface(
            TaskResponse,
            "TaskRecord",
            field_overrides={
                "status": "TaskStatus",
                "execution_status": "TaskStatus | null",
            },
        )
        + "\n"
    )


def render_turn_types() -> str:
    """渲染轮次共享类型。

    参数:
        无。

    返回:
        ``turn.ts`` 内容。

    异常:
        无。

    副作用:
        无。
    """

    statuses = "\n".join(f'  | "{status.value}"' for status in TurnStatus)
    return (
        generated_header("turn")
        + "\n"
        + f"export type TurnStatus =\n{statuses};\n\n"
        + render_interface(
            TurnResponse, "TurnRecord", field_overrides={"status": "TurnStatus"}
        )
        + "\n"
    )


def render_api_types() -> str:
    """渲染 API 路径、请求和响应类型。

    参数:
        无。

    返回:
        ``api.ts`` 内容。

    异常:
        无。

    副作用:
        无。
    """

    body = "\n\n".join(
        [
            render_api_paths(),
            render_interface(CreateTaskRequest),
            render_interface(CreateWorkspaceRequest),
            render_interface(CreateTurnRequest),
            render_interface(HealthResponse, "BackendHealthResponse"),
            render_interface(DeleteWorkspaceResponse),
            'export type TaskResponse = import("./task").TaskRecord;',
            'export type WorkspaceResponse = import("./workspace").WorkspaceRecord;',
            'export type TurnResponse = import("./turn").TurnRecord;',
            'export type LogQueryResponse = import("./logs").LogQueryResponse;',
            'export type AgentProfileResponse = import("./agents").AgentProfileResponse;',
            'export type ListAgentsResponse = import("./agents").ListAgentsResponse;',
        ]
    )
    return generated_header("api") + "\n" + body + "\n"


def render_logs_types() -> str:
    """渲染日志查询共享类型。

    参数:
        无。

    返回:
        ``logs.ts`` 内容。

    异常:
        无。

    副作用:
        无。
    """

    parts = [
        generated_header("logs"),
        "export type LogLevel = string;",
        render_interface(QueryLogsRequest),
        render_interface(RecentLogsRequest),
        """export interface LogError extends Record<string, unknown> {
  type?: string;
  message?: string;
  stack?: string;
}""",
        render_interface(
            LogEntryResponse, field_overrides={"error": "LogError | null"}
        ),
        render_interface(LogQueryResponse),
        """export interface LogQueryRequest {
  trace_id?: string;
  level?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
}""",
    ]
    return "\n\n".join(parts) + "\n"


def render_api_paths() -> str:
    """渲染手工维护的 API 路径常量。

    参数:
        无。

    返回:
        ``API_BASE`` 与 ``API_PATHS`` TypeScript 代码。

    异常:
        无。

    副作用:
        无。
    """

    return """export const API_BASE = "/api";

export const API_PATHS = {
  HEALTH: "/health",
  WORKSPACES: "/workspaces",
  WORKSPACE_DETAIL: (workspaceId: string) => `/workspaces/${workspaceId}`,
  WORKSPACE_TASKS: (workspaceId: string) => `/workspaces/${workspaceId}/tasks`,
  TASK_DETAIL: (taskId: string) => `/tasks/${taskId}`,
  TASK_TURNS: (taskId: string) => `/tasks/${taskId}/turns`,
  TURN_STREAM: (turnId: string) => `/turns/${turnId}/stream`,
  TURN_CANCEL: (turnId: string) => `/turns/${turnId}/cancel`,
  LOGS_QUERY: "/logs/query",
  LOGS_RECENT: "/logs/recent",
} as const;"""


def render_module(
    models: Sequence[type[BaseModel]],
    module_name: str,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """渲染一个由 Pydantic 模型组成的 TS 模块。

    参数:
        models: 要渲染的模型列表。
        module_name: 输出模块名，用于文件头注释。
        aliases: 可选别名映射，key 为导出的别名，value 为目标类型名。

    返回:
        TypeScript 模块内容。

    异常:
        无。

    副作用:
        无。
    """

    parts = [generated_header(module_name)]
    parts.extend(render_interface(model) for model in models)
    for alias, target in (aliases or {}).items():
        parts.append(f"export type {alias} = {target};")
    return "\n\n".join(parts) + "\n"


def render_interface(
    model: type[BaseModel],
    name: str | None = None,
    field_overrides: Mapping[str, str] | None = None,
) -> str:
    """渲染 Pydantic 模型为 TypeScript interface。

    参数:
        model: 待渲染的 Pydantic 模型。
        name: 可选 TS interface 名；默认使用 Python 类名。
        field_overrides: 字段级 TS 类型覆盖。

    返回:
        TypeScript interface 文本。

    异常:
        无。

    副作用:
        无。
    """

    overrides = field_overrides or {}
    lines = [f"export interface {name or model.__name__} {{"]
    for field_name, field_info in model.model_fields.items():
        optional = "?" if not field_info.is_required() else ""
        field_type = overrides.get(field_name) or ts_type(field_info.annotation)
        lines.append(f"  {field_name}{optional}: {field_type};")
    lines.append("}")
    return "\n".join(lines)


def ts_type(annotation: Any) -> str:
    """把 Python 类型注解转换成 TypeScript 类型表达式。

    参数:
        annotation: Pydantic 字段注解。

    返回:
        TypeScript 类型表达式。

    异常:
        无。

    副作用:
        无。
    """

    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation is Any:
        return "unknown"
    if annotation is str:
        return "string"
    if annotation is int or annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"
    if annotation is type(None):
        return "null"
    if origin is list:
        return f"{ts_type(args[0])}[]"
    if origin is dict:
        return "Record<string, unknown>"
    if origin in (UnionType, getattr(sys.modules["typing"], "Union", object())):
        return " | ".join(ts_type(arg) for arg in args)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__
    return "unknown"


def generated_header(module_name: str) -> str:
    """渲染生成文件头。

    参数:
        module_name: 模块名。

    返回:
        文件头注释。

    异常:
        无。

    副作用:
        无。
    """

    return f"""/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/{module_name}
 */"""


if __name__ == "__main__":
    main()

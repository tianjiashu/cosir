"""Generate shared TypeScript API types from backend Pydantic schemas."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import UnionType
from typing import Any, get_args, get_origin

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "apps" / "backend"
OPENAPI_DOC_PATH = REPO_ROOT / "apps" / "shared" / "fastapi_docs.json"
SHARED_TS_ROOT = REPO_ROOT / "apps" / "shared" / "ts"

API_PATH_TEMPLATES: Mapping[str, str] = {
    "/health": "/health",
    "/agents": "/agents",
    "/workspaces": "/workspaces",
    "/workspaces/{workspace_id}": "/workspaces/${workspaceId}",
    "/workspaces/{workspace_id}/tasks": "/workspaces/${workspaceId}/tasks",
    "/workspaces/{workspace_id}/events/stream": "/workspaces/${workspaceId}/events/stream",
    "/workspaces/{workspace_id}/events/prepare": "/workspaces/${workspaceId}/events/prepare",
    "/tasks/{task_id}": "/tasks/${taskId}",
    "/tasks/{task_id}/turns": "/tasks/${taskId}/turns",
    "/tasks/{task_id}/events": "/tasks/${taskId}/events",
    "/tasks/{task_id}/turns/{turn_id}/events": "/tasks/${taskId}/turns/${turnId}/events",
    "/turns/{turn_id}/stream": "/turns/${turnId}/stream",
    "/turns/{turn_id}/cancel": "/turns/${turnId}/cancel",
    "/tasks/{task_id}/changes": "/tasks/${taskId}/changes",
    "/tasks/{task_id}/changes/keep": "/tasks/${taskId}/changes/keep",
    "/tasks/{task_id}/changes/revert": "/tasks/${taskId}/changes/revert",
    "/logs/query": "/logs/query",
    "/logs/recent": "/logs/recent",
}

sys.path.insert(0, str(BACKEND_ROOT))

from app.api.schemas.request.CreateTaskRequest import CreateTaskRequest  # noqa: E402
from app.api.schemas.request.CreateTurnRequest import CreateTurnRequest  # noqa: E402
from app.api.schemas.request.CreateWorkspaceRequest import CreateWorkspaceRequest  # noqa: E402
from app.api.schemas.request.QueryLogsRequest import QueryLogsRequest  # noqa: E402
from app.api.schemas.request.RecentLogsRequest import RecentLogsRequest  # noqa: E402
from app.api.schemas.response.AgentProfileResponse import AgentProfileResponse  # noqa: E402
from app.api.schemas.response.ChangeSetResponse import (  # noqa: E402
    ChangeCheckpointResponse,
    ChangeFileResponse,
    ChangeSetResponse,
)
from app.api.schemas.response.DeleteTaskResponse import DeleteTaskResponse  # noqa: E402
from app.api.schemas.response.DeleteWorkspaceResponse import DeleteWorkspaceResponse  # noqa: E402
from app.api.schemas.response.HealthResponse import HealthResponse  # noqa: E402
from app.api.schemas.response.ListAgentsResponse import ListAgentsResponse  # noqa: E402
from app.api.schemas.response.LogEntryResponse import LogEntryResponse  # noqa: E402
from app.api.schemas.response.LogQueryResponse import LogQueryResponse  # noqa: E402
from app.api.schemas.response.TaskResponse import TaskResponse  # noqa: E402
from app.api.schemas.response.TurnResponse import TurnResponse  # noqa: E402
from app.api.schemas.response.WorkspaceResponse import WorkspaceResponse  # noqa: E402
from app.api.schemas.response.WorkspacePrepareResponse import (  # noqa: E402
    WorkspacePrepareResponse,
)
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
            [WorkspaceResponse, WorkspacePrepareResponse],
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
    write("changes.ts", render_change_types())
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
            render_interface(DeleteTaskResponse),
            'export type TaskResponse = import("./task").TaskRecord;',
            'export type WorkspaceResponse = import("./workspace").WorkspaceRecord;',
            'export type WorkspacePrepareResponse = import("./workspace").WorkspacePrepareResponse;',
            'export type TurnResponse = import("./turn").TurnRecord;',
            'export type ChangeSet = import("./changes").ChangeSet;',
            'export type ChangeFile = import("./changes").ChangeFile;',
            'export type ChangeCheckpoint = import("./changes").ChangeCheckpoint;',
            'export type LogQueryResponse = import("./logs").LogQueryResponse;',
            'export type AgentProfileResponse = import("./agents").AgentProfileResponse;',
            'export type ListAgentsResponse = import("./agents").ListAgentsResponse;',
        ]
    )
    return generated_header("api") + "\n" + body + "\n"


def render_change_types() -> str:
    """渲染变更集共享类型（``ChangeFile`` / ``ChangeCheckpoint`` / ``ChangeSet``）。

    后端响应模型命名为 ``Change*Response``，此处按前端消费习惯重命名为无
    ``Response`` 后缀的 ``ChangeFile`` / ``ChangeCheckpoint`` / ``ChangeSet``，
    并通过字段覆盖把嵌套列表类型对齐到同名接口。

    参数:
        无。

    返回:
        ``changes.ts`` 内容。

    异常:
        无。

    副作用:
        无。
    """

    parts = [
        generated_header("changes"),
        render_interface(
            ChangeFileResponse,
            "ChangeFile",
            field_overrides={
                "status": "'pending' | 'kept' | 'reverted'",
            },
        ),
        render_interface(ChangeCheckpointResponse, "ChangeCheckpoint"),
        render_interface(
            ChangeSetResponse,
            "ChangeSet",
            field_overrides={
                "checkpoints": "ChangeCheckpoint[]",
                "files": "ChangeFile[]",
            },
        ),
    ]
    return "\n\n".join(parts) + "\n"


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
    """渲染 API 路径常量，并校验 OpenAPI 路径覆盖。

    参数:
        无。

    返回:
        ``API_BASE`` 与 ``API_PATHS`` TypeScript 代码。

    异常:
        无。

    副作用:
        无。
    """

    validate_openapi_paths_are_declared()
    return """export const API_BASE = "/api";

export const API_PATHS = {
  HEALTH: "/health",
  AGENTS: "/agents",
  WORKSPACES: "/workspaces",
  WORKSPACE_DETAIL: (workspaceId: string) => `/workspaces/${workspaceId}`,
  WORKSPACE_TASKS: (workspaceId: string) => `/workspaces/${workspaceId}/tasks`,
  WORKSPACE_EVENT_STREAM: (workspaceId: string) => `/workspaces/${workspaceId}/events/stream`,
  WORKSPACE_EVENT_PREPARE: (workspaceId: string) => `/workspaces/${workspaceId}/events/prepare`,
  TASK_DETAIL: (taskId: string) => `/tasks/${taskId}`,
  TASK_TURNS: (taskId: string) => `/tasks/${taskId}/turns`,
  TASK_EVENTS: (taskId: string) => `/tasks/${taskId}/events`,
  TASK_CHANGES: (taskId: string) => `/tasks/${taskId}/changes`,
  TASK_CHANGES_REVERT: (taskId: string) => `/tasks/${taskId}/changes/revert`,
  TASK_CHANGES_KEEP: (taskId: string) => `/tasks/${taskId}/changes/keep`,
  TURN_STREAM: (turnId: string) => `/turns/${turnId}/stream`,
  TURN_CANCEL: (turnId: string) => `/turns/${turnId}/cancel`,
  LOGS_QUERY: "/logs/query",
  LOGS_RECENT: "/logs/recent",
} as const;"""


def validate_openapi_paths_are_declared() -> None:
    """校验 OpenAPI 快照里的路径都已声明到前端路径表。

    参数:
        无。

    返回:
        无。

    异常:
        RuntimeError: 当 OpenAPI 快照不存在、格式异常或存在未声明路径时抛出。

    副作用:
        读取 ``apps/shared/fastapi_docs.json``。
    """

    try:
        raw_doc = json.loads(OPENAPI_DOC_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(
            f"OpenAPI 快照不存在，无法校验 API 路径：{OPENAPI_DOC_PATH}"
        ) from exc
    paths = raw_doc.get("paths")
    if not isinstance(paths, dict):
        raise RuntimeError("OpenAPI 快照缺少 paths 字典，无法校验 API 路径")

    missing = sorted(set(paths) - set(API_PATH_TEMPLATES))
    if missing:
        raise RuntimeError(f"API_PATHS 缺少 OpenAPI 路径声明：{', '.join(missing)}")


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

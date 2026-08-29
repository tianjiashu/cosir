"""生成 workspace 状态事件 payload 的 TypeScript 共享类型。

后端 ``app/models/payload/workspace_payload/`` 下的三个 Pydantic payload 模型
（``WorkspacePreparingPayload`` / ``WorkspaceReadyPayload`` / ``WorkspaceDegradedPayload``）
此前由前端 ``shared/ts/workspaceEvent.ts`` **手写**，存在与后端漂移的风险（与
``events.ts`` 同源问题）。本脚本从后端模型生成 payload 接口与联合类型，写入
``apps/shared/ts/workspacePayload.ts`` 作为唯一事实来源；``workspaceEvent.ts`` 仅
import 本文件并对齐（不再手写 payload 字段）。

信封（``WorkspaceEvent``）、``WorkspaceEventType`` 字面量联合、前端状态机
（``WorkspaceState`` / ``WorkspaceDegradedState`` / ``WorkspaceStatus``）与工具函数
属于前端语义层，不在本脚本范围，仍保留在 ``workspaceEvent.ts`` 手写。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from types import UnionType
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "apps" / "backend"
SHARED_TS_ROOT = REPO_ROOT / "apps" / "shared" / "ts"

sys.path.insert(0, str(BACKEND_ROOT))

from app.models.payload.workspace_payload.workspace_degraded_payload import (  # noqa: E402
    WorkspaceDegradedPayload,
)
from app.models.payload.workspace_payload.workspace_preparing_payload import (  # noqa: E402
    WorkspacePreparingPayload,
)
from app.models.payload.workspace_payload.workspace_ready_payload import (  # noqa: E402
    WorkspaceReadyPayload,
)


def main() -> None:
    """生成 workspace payload 的 TypeScript 类型。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        覆盖写入 ``apps/shared/ts/workspacePayload.ts``。
    """

    content = render_workspace_payload_types()
    (SHARED_TS_ROOT / "workspacePayload.ts").write_text(content, encoding="utf-8")


def render_workspace_payload_types() -> str:
    """渲染 workspace 状态事件 payload 共享类型。

    参数:
        无。

    返回:
        ``workspacePayload.ts`` 内容（三个 payload interface + 联合类型）。

    异常:
        无。

    副作用:
        无。
    """

    payload_models: Sequence[type[BaseModel]] = [
        WorkspacePreparingPayload,
        WorkspaceReadyPayload,
        WorkspaceDegradedPayload,
    ]
    interfaces = "\n\n".join(render_interface(model) for model in payload_models)
    union = "  | ".join(model.__name__ for model in payload_models)
    return (
        generated_header("workspacePayload")
        + "\n"
        + interfaces
        + "\n\nexport type WorkspacePayload =\n  | "
        + union
        + ";\n"
    )


def render_interface(model: type[BaseModel], name: str | None = None) -> str:
    """渲染 Pydantic 模型为 TypeScript interface。

    参数:
        model: 待渲染的 Pydantic 模型。
        name: 可选 TS interface 名；默认使用 Python 类名。

    返回:
        TypeScript interface 文本。

    异常:
        无。

    副作用:
        无。
    """

    lines = [f"export interface {name or model.__name__} {{"]
    for field_name, field_info in model.model_fields.items():
        optional = "?" if not field_info.is_required() else ""
        field_type = ts_type(field_info.annotation)
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
    if origin is Literal:
        return " | ".join(repr(arg) for arg in args)
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
 * 后端 workspace 状态事件 payload 共享类型定义。
 *
 * 本文件由 `scripts/generate_workspace_event_ts.py` 从后端 Pydantic payload 模型生成。
 * 不要手动修改；请先更新 `apps/backend/app/models/payload/workspace_payload/` 后重新生成。
 *
 * @module shared/{module_name}
 */"""


if __name__ == "__main__":
    main()

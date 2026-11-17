"""浏览器工具调用宿主 ``browser.*`` 方法的唯一入口：注入归属 Session、标签上下文与交接约束。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.core.tool.handoff.handoff_registry import handoff_registry
from app.core.tool.schemas import ToolExecutionContext
from app.tools.browser_runtime.deadlines import host_timeout
from app.tools.browser_runtime.tab_context import BrowserTabContext
from app.tools.host.host_call import call_host


def call_browser(
    method: str,
    execution_context: ToolExecutionContext,
    **params: Any,
) -> Any:
    """调用宿主浏览器方法，失败以 ``HostCallError`` 上浮（见 ``call_host``）。

    每个请求都带 ``session_id`` 与标签上下文（上限与下载目录，见 ``tab_context``）：宿主每次都据此
    刷新上限（页面自己开出的标签没有请求可带，按最近一次请求的上限算），下载目录只在这次请求
    新建标签时用。宿主去掉这三个键后按导出的参数 schema（``tool-args.schema.json``）校验其余
    参数：它要求参数模型的每个字段都在，所以调用方原样传参数模型的全部字段（未填的为 ``None``，
    宿主按「未给」理解，如没有 ``tab`` 即用当前标签），不在这里过滤。
    """

    request = {
        "session_id": execution_context.session_id,
        **asdict(BrowserTabContext.from_workspace(execution_context.workspace_root)),
        **params,
        "handoff_tabs": [
            lease.resource_id for lease in handoff_registry.leases(execution_context.session_id)
        ],
    }
    return call_host(
        method, request, execution_context=execution_context, timeout_seconds=host_timeout(method)
    )

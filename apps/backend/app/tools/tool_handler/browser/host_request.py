"""浏览器工具调用宿主 ``browser.*`` 方法的唯一入口：注入归属 Task 与标签上下文。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Final

from app.core.tool.schemas import ToolExecutionContext
from app.service.browser.tab_context import BrowserTabContext
from app.tools.host.host_call import call_host

# 宿主给每个方法的总时限（秒），宿主保证在这之内答复（超时也答复，码为 ``DEADLINE_EXCEEDED``）。
# 唯一来源是宿主的 ``contract/method-deadlines.json``；这里是镜像（后端运行时不读 desktop 目录），
# 由 ``tests/test_browser_contract_guard.py`` 核对。后端不按宿主内部各步的超时推算最坏耗时。
HOST_DEADLINES: Final[dict[str, float]] = {
    "browser.act": 330,
    "browser.evaluate": 20,
    "browser.logs": 5,
    "browser.navigate": 60,
    "browser.screenshot": 30,
    "browser.snapshot": 30,
    "browser.tabs": 5,
}
# 链路往返的余量：后端比宿主多等这么多，宿主的答复（包括超时答复）总能先到，模型拿得到做到了哪一步。
_LINK_MARGIN_SECONDS = 5.0
# 工具线程自己的收尾（如截图存成附件）。
_TOOL_MARGIN_SECONDS = 10.0


def host_timeout(method: str) -> float:
    """后端等一次宿主调用的时限。"""

    return HOST_DEADLINES[method] + _LINK_MARGIN_SECONDS


def tool_timeout(*methods: str) -> float:
    """工具的执行时限：一次执行里依次调用的这些方法的等待时限之和，再加工具自己收尾的余量。"""

    return sum(host_timeout(method) for method in methods) + _TOOL_MARGIN_SECONDS


def call_browser(
    method: str,
    execution_context: ToolExecutionContext,
    **params: Any,
) -> Any:
    """调用宿主浏览器方法，失败以 ``HostCallError`` 上浮（见 ``call_host``）。

    每个请求都带 ``task_id`` 与标签上下文（上限与下载目录，见 ``tab_context``）：宿主每次都据此
    刷新上限（页面自己开出的标签没有请求可带，按最近一次请求的上限算），下载目录只在这次请求
    新建标签时用。宿主去掉这三个键后按导出的参数 schema（``tool-args.schema.json``）校验其余
    参数：它要求参数模型的每个字段都在，所以调用方原样传参数模型的全部字段（未填的为 ``None``，
    宿主按「未给」理解，如没有 ``tab`` 即用当前标签），不在这里过滤。
    """

    request = {
        "task_id": execution_context.task_id,
        **asdict(BrowserTabContext.from_workspace(execution_context.workspace_root)),
        **params,
    }
    return call_host(
        method, request, execution_context=execution_context, timeout_seconds=host_timeout(method)
    )

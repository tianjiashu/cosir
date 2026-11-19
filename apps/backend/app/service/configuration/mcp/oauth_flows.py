"""配置页「登录授权」的交互流程：两段式交给前端。

连接测试从不交互（需要授权时只得出「需要授权」）；只有用户点了「登录授权」才走这里。用户要去浏览器
里登录，HTTP 请求等不了几分钟、也没法中途把授权地址推给前端，所以拆成两步：:meth:`MCPAuthorizationFlows.begin`
开回调监听并连接服务，在拿到授权地址前就结束（已有可用令牌、或服务其实不要求授权）时直接返回结果；拿到
授权地址时登记这次授权、返回 :class:`PendingAuthorization`，在后台继续等回调。前端打开授权地址后调用
:meth:`MCPAuthorizationFlows.wait` 取最终结果，放弃时调用 :meth:`MCPAuthorizationFlows.cancel`。

进程内协调状态，不持久化、不跨重启（重启后用户重新点「登录授权」即可）。同一个服务同一时刻只有一次授权：
再次授权会取消上一次（它占着回调端口）。没被取走结果的已结束授权留到同一服务下次授权时清掉。

每个授权都有收尾：``begin`` 的请求被取消（前端断开）时后台任务一并取消，不会挂满时限后再写记录；删除
工作区时按作用域取消（:meth:`MCPAuthorizationFlows.cancel_scope`），迟到的结果不会把已删除作用域的记录写回来。
只是配置页的编排、没有工具使用，所以在 service 层；建连时用到的回调监听与 provider 仍在 ``tools/mcp/oauth``。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config.logging.logger import log
from app.tools.mcp.oauth.callback_listener import OAuthCallbackListener, OAuthCallbackTimeout
from app.tools.mcp.oauth.provider import AuthorizationInteraction

if TYPE_CHECKING:
    from app.service.configuration.mcp.configuration_service import MCPServerTestResult

# 用户在浏览器里完成授权的时限。
AUTHORIZATION_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class PendingAuthorization:
    """测试停在授权这一步：用户需要打开 ``authorization_url``，再按 ``flow_id`` 取结果。"""

    flow_id: str
    authorization_url: str


@dataclass
class _Flow:
    server_key: tuple[str, str, str]
    task: asyncio.Task[MCPServerTestResult]


class MCPAuthorizationFlows:
    """按 ``flow_id`` 登记进行中的交互授权。只在后端事件循环上使用；配置服务持有一份进程级实例。"""

    def __init__(self) -> None:
        self._flows: dict[str, _Flow] = {}

    async def begin(
        self,
        server_key: tuple[str, str, str],
        callback_port: int | None,
        run: Callable[[AuthorizationInteraction], Awaitable[MCPServerTestResult]],
        *,
        timeout_before_authorization: float,
    ) -> MCPServerTestResult | PendingAuthorization:
        """开一个回调监听并运行 ``run``；先结束就返回结果，先拿到授权地址就转入后台等用户授权。

        异常:
            TimeoutError: 在时限内既没结束也没拿到授权地址（连接或发现卡住）；已取消。
            OSError: 回调端口被占用（固定了 ``callback_port`` 时可能发生）。
            CancelledError: 等待途中请求被取消（前端断开）；后台任务一并取消、不登记。
            其余: ``run`` 自身的异常原样上浮。
        """

        self._cancel_server(server_key)
        authorization_url: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def offer(url: str) -> None:
            if not authorization_url.done():
                authorization_url.set_result(url)

        async def body() -> MCPServerTestResult:
            async with OAuthCallbackListener(callback_port) as listener:

                async def wait_callback() -> tuple[str, str | None]:
                    try:
                        async with asyncio.timeout(AUTHORIZATION_TIMEOUT_SECONDS):
                            return await listener.wait()
                    except TimeoutError:
                        raise OAuthCallbackTimeout from None

                return await run(AuthorizationInteraction(listener.redirect_uri, offer, wait_callback))

        task = asyncio.get_running_loop().create_task(body())
        try:
            done, _ = await asyncio.wait(
                {task, authorization_url},
                timeout=timeout_before_authorization,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            # 还没登记，没有别人会收尾：不取消就挂满授权时限，之后照常写记录。
            task.cancel()
            authorization_url.cancel()
            raise
        if task in done:
            authorization_url.cancel()
            return task.result()
        if authorization_url in done:
            flow_id = uuid.uuid4().hex
            self._flows[flow_id] = _Flow(server_key, task)
            log.info(
                "mcp_oauth_authorization_started",
                extra={
                    "msg": "MCP 服务登录授权已开始，交给前端打开授权页",
                    "data": {"scope": server_key[0], "server_name": server_key[2], "flow_id": flow_id},
                },
            )
            return PendingAuthorization(flow_id, authorization_url.result())
        task.cancel()
        authorization_url.cancel()
        raise TimeoutError

    async def wait(self, flow_id: str) -> MCPServerTestResult:
        """等一次授权的最终结果（测试自身的结果）。

        异常:
            KeyError: 没有这次授权（已被取走、被取消或顶替，或后端重启过）；等待途中被取消也按此报告，
                否则正在等的请求只会收到一个裸的 ``CancelledError``。
        """

        flow = self._flows[flow_id]
        try:
            return await asyncio.shield(flow.task)
        except asyncio.CancelledError:
            if flow.task.cancelled():
                raise KeyError(flow_id) from None
            raise
        finally:
            if flow.task.done() and self._flows.get(flow_id) is flow:
                del self._flows[flow_id]

    def cancel(self, flow_id: str) -> None:
        """用户放弃这次授权：结束等待、关掉回调监听。

        异常:
            KeyError: 没有这次授权（已结束并被取走、被再次授权顶替，或后端重启过）。
        """

        flow = self._flows.pop(flow_id)
        flow.task.cancel()
        log.info(
            "mcp_oauth_authorization_cancelled",
            extra={
                "msg": "用户取消了 MCP 服务的登录授权",
                "data": {"scope": flow.server_key[0], "server_name": flow.server_key[2], "flow_id": flow_id},
            },
        )

    def cancel_scope(self, scope_key: str) -> None:
        """取消一个作用域里进行中的全部授权（删除工作区前调用）：迟到的结果不再写回已删除作用域的记录。"""

        for flow_id, flow in list(self._flows.items()):
            if flow.server_key[0] == scope_key:
                flow.task.cancel()
                del self._flows[flow_id]
                log.info(
                    "mcp_oauth_authorization_dropped",
                    extra={
                        "msg": "作用域被删除，进行中的 MCP 登录授权已取消",
                        "data": {"scope": scope_key, "server_name": flow.server_key[2], "flow_id": flow_id},
                    },
                )

    def _cancel_server(self, server_key: tuple[str, str, str]) -> None:
        for flow_id, flow in list(self._flows.items()):
            if flow.server_key == server_key:
                flow.task.cancel()
                del self._flows[flow_id]


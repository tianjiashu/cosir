"""配置页登录授权流程的收尾：请求被取消、作用域被删除时，后台授权不会挂着、也不会迟到写记录。"""

from __future__ import annotations

import asyncio

import pytest

from app.service.configuration.mcp.oauth_flows import MCPAuthorizationFlows, PendingAuthorization

_KEY = ("workspace:/repo", "workspace:/repo", "remote")


def _never_finishes(stopped: list[str], *, offer_url: bool):
    async def run(interaction):
        try:
            if offer_url:
                await interaction.on_redirect("https://auth.example/authorize")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append("cancelled")
            raise

    return run


# 潜在缺陷：前端断开时 begin 被取消，后台任务既没取消也没登记，挂满授权时限后还照常写记录。
def test_cancelling_begin_cancels_the_background_flow() -> None:
    async def scenario() -> list[str]:
        flows = MCPAuthorizationFlows()
        stopped: list[str] = []
        begin = asyncio.create_task(
            flows.begin(_KEY, None, _never_finishes(stopped, offer_url=False), timeout_before_authorization=10)
        )
        await asyncio.sleep(0.1)
        begin.cancel()
        with pytest.raises(asyncio.CancelledError):
            await begin
        await asyncio.sleep(0.05)
        # 在事件循环还在跑时检查：结束时 asyncio.run 会自己取消残留任务，那时再看就分不出来。
        return list(stopped)

    assert asyncio.run(scenario()) == ["cancelled"]


# 潜在缺陷：删除工作区不取消它名下的授权，迟到的结果把已删除作用域的记录写回来。
def test_cancelling_a_scope_ends_its_pending_sign_ins_only() -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        flows = MCPAuthorizationFlows()
        stopped: list[str] = []
        others: list[str] = []
        pending = await flows.begin(
            _KEY, None, _never_finishes(stopped, offer_url=True), timeout_before_authorization=10
        )
        other = await flows.begin(
            ("system", "system", "remote"), None, _never_finishes(others, offer_url=True),
            timeout_before_authorization=10,
        )
        assert isinstance(pending, PendingAuthorization) and isinstance(other, PendingAuthorization)

        flows.cancel_scope(_KEY[0])
        await asyncio.sleep(0.05)
        cancelled_in_scope, still_running = list(stopped), list(others)

        with pytest.raises(KeyError):
            await flows.wait(pending.flow_id)
        flows.cancel(other.flow_id)  # 别的作用域不受影响，仍在登记表里
        return cancelled_in_scope, still_running

    assert asyncio.run(scenario()) == (["cancelled"], [])

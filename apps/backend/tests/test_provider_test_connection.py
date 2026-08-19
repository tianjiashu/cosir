"""厂商连通性测试服务单元测试。

覆盖设计文档阶段 2 三件套之一 + 阶段 4 错误码归一接线：
- 成功：``test_connection`` 返回 ``success=True``（不抛异常）；
- 认证失败：``AuthenticationError`` 经 mapper 归一为 ``model_auth_failed``，
  结果 ``success=False`` + ``error_code``（200 语义，不抛 HTTP 异常）；
- 网络错误：``APIConnectionError`` 归一为 ``model_network_error``。

通过 mock 模块级 ``_acompletion_ping`` 模拟 litellm 调用，不触发真实网络。
"""

from unittest.mock import patch

import litellm.exceptions as litellm_exc

from app.models import ProviderRecord
from app.service.provider.provider_connection_test_service import (
    ProviderConnectionTestService,
)
from app.utils.datetime_utils import utc_now


def _make_provider(provider_type: str = "deepseek") -> ProviderRecord:
    """构造测试用 ProviderRecord（不落库）。

    参数:
        provider_type: 厂商类型键（默认 deepseek）。

    返回:
        不落库的 ProviderRecord 实例。

    异常:
        无。

    副作用:
        无。
    """

    now = utc_now()
    return ProviderRecord(
        provider_id="p-test",
        name=provider_type,
        provider_type=provider_type,
        created_at=now,
        updated_at=now,
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        enabled=True,
        sort_order=0,
    )


async def _succeed(**kwargs) -> None:
    """模拟成功的 litellm 调用（无副作用）。"""
    return None


def test_connection_success_returns_success_result() -> None:
    """连通性测试成功：返回 success=True，不抛异常。"""

    async def run() -> None:
        service = ProviderConnectionTestService()
        with patch(
            "app.service.provider.provider_connection_test_service._acompletion_ping",
            side_effect=_succeed,
        ):
            result = await service.test_connection(_make_provider())

        assert result.success is True
        assert result.error_code is None
        assert result.provider_id == "p-test"

    # 直接用 asyncio 跑协程（pytest 无 pytest-asyncio 插件时）
    import asyncio

    asyncio.run(run())


def test_connection_auth_failure_maps_to_auth_failed() -> None:
    """认证失败：AuthenticationError → error_code=model_auth_failed，success=False。"""

    async def _raise_auth(**kwargs) -> None:
        raise litellm_exc.AuthenticationError(
            message="invalid api key", llm_provider="deepseek", model="deepseek-chat"
        )

    async def run() -> None:
        service = ProviderConnectionTestService()
        with patch(
            "app.service.provider.provider_connection_test_service._acompletion_ping",
            side_effect=_raise_auth,
        ):
            result = await service.test_connection(_make_provider())

        assert result.success is False
        assert result.error_code == "model_auth_failed"
        assert result.error_message is not None

    import asyncio

    asyncio.run(run())


def test_connection_network_error_maps_to_network() -> None:
    """网络错误：APIConnectionError → error_code=model_network_error，success=False。"""

    async def _raise_conn(**kwargs) -> None:
        raise litellm_exc.APIConnectionError(
            message="connection refused", llm_provider="deepseek", model="deepseek-chat"
        )

    async def run() -> None:
        service = ProviderConnectionTestService()
        with patch(
            "app.service.provider.provider_connection_test_service._acompletion_ping",
            side_effect=_raise_conn,
        ):
            result = await service.test_connection(_make_provider())

        assert result.success is False
        assert result.error_code == "model_network_error"
        assert result.error_message is not None

    import asyncio

    asyncio.run(run())

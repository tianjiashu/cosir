"""厂商连通性测试对抗性边界测试（独立测试 Agent 新增）。

目标：挖掘 ``ProviderConnectionTestService.test_connection`` 在异常分支的降级缺陷。
- 未知/非 litellm 异常 → MODEL_UNKNOWN（success=False，不抛）；
- custom 厂商（无前缀）→ 测试模型名回退 ``ping``；
- 失败结果保持 200 语义（返回 ConnectionTestResult，不抛 HTTP 异常）；
- elapsed_ms 为非负整数；
- 日志记录了可排查上下文（provider_id / error_code）。
"""

import asyncio
from unittest.mock import patch

import litellm.exceptions as litellm_exc

from app.service.llm.model_error_mapper import map_litellm_error
from app.models import ProviderRecord
from app.service.provider.provider_connection_test_service import (
    ProviderConnectionTestService,
)
from app.utils.datetime_utils import utc_now


def _make_provider(provider_type: str = "deepseek") -> ProviderRecord:
    now = utc_now()
    return ProviderRecord(
        provider_id="p-test",
        name=provider_type,
        provider_type=provider_type,
        created_at=now,
        updated_at=now,
        base_url="https://api.example.com",
        api_key="sk-test",
        enabled=True,
        sort_order=0,
    )


def test_unknown_exception_maps_to_model_unknown() -> None:
    """未知异常（ValueError）→ error_code=model_unknown，success=False，不抛。"""

    async def _raise_value(**kwargs) -> None:
        raise ValueError("unexpected internal error")

    async def run() -> None:
        service = ProviderConnectionTestService()
        with patch(
            "app.service.provider.provider_connection_test_service._acompletion_ping",
            side_effect=_raise_value,
        ):
            result = await service.test_connection(_make_provider())

        assert result.success is False
        assert result.error_code == "model_unknown"
        assert result.error_message is not None
        assert result.elapsed_ms >= 0

    asyncio.run(run())


def test_custom_provider_test_model_name_is_ping() -> None:
    """custom 厂商（无 litellm_prefix）→ 测试模型名回退 ``ping``。"""

    captured: dict = {}

    async def _capture(**kwargs) -> None:
        captured.update(kwargs)

    async def run() -> None:
        service = ProviderConnectionTestService()
        with patch(
            "app.service.provider.provider_connection_test_service._acompletion_ping",
            side_effect=_capture,
        ):
            result = await service.test_connection(_make_provider("custom"))

        assert result.success is True
        assert captured["model"] == "ping"

    asyncio.run(run())


def test_deepseek_test_model_name_prefixed() -> None:
    """deepseek 厂商 → 测试模型名为 ``deepseek/ping``（含前缀）。"""

    captured: dict = {}

    async def _capture(**kwargs) -> None:
        captured.update(kwargs)

    async def run() -> None:
        service = ProviderConnectionTestService()
        with patch(
            "app.service.provider.provider_connection_test_service._acompletion_ping",
            side_effect=_capture,
        ):
            result = await service.test_connection(_make_provider("deepseek"))

        assert result.success is True
        assert captured["model"] == "deepseek/ping"
        # api_base / api_key 应透传（验证用户配置而非 litellm 内置）
        assert captured["api_base"] == "https://api.example.com"
        assert captured["api_key"] == "sk-test"

    asyncio.run(run())


def test_mapper_never_raises_for_arbitrary_exc() -> None:
    """map_litellm_error 对所有 BaseException 输入不抛（含字符串异常等）。"""
    for exc in (
        RuntimeError("x"),
        ValueError("y"),
        BaseException("z"),
        litellm_exc.AuthenticationError(message="a", llm_provider="x", model="m"),
    ):
        info = map_litellm_error(exc)
        assert info.error_code.value  # 稳定字符串
        assert isinstance(info.guidance, str)

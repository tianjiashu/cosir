"""``ToolRegistry.register`` 的装配期校验：静态执行契约在注册时一次拦截，执行层不再逐次防御。"""

import pytest
from pydantic import BaseModel

from app.core.tools.schemas import ToolDefinition
from app.core.tools.tool_registry import ToolRegistry


class _NoArgs(BaseModel):
    """空参数模型。"""


def _handler(**_kwargs: object) -> str:
    return "ok"


def _definition(**overrides: object) -> ToolDefinition:
    fields: dict[str, object] = {
        "name": "probe",
        "group": "probe",
        "description": "registry contract probe",
        "handler": _handler,
        "args_model": _NoArgs,
    }
    fields.update(overrides)
    return ToolDefinition(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0])
def test_process_tool_with_non_positive_timeout_is_rejected(timeout_seconds: float) -> None:
    """process 工具的硬超时是强杀的唯一依据：非正数在注册时就失败，不留到执行期。"""

    registry = ToolRegistry()
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        registry.register(_definition(execution_mode="process", timeout_seconds=timeout_seconds))
    assert registry.get_tool_definition("probe") is None


def test_thread_tool_timeout_is_metadata_only() -> None:
    """thread 工具不做硬超时强杀，``timeout_seconds`` 只是元数据，注册不校验它。"""

    registry = ToolRegistry()
    registry.register(_definition(execution_mode="thread", timeout_seconds=0.0))
    assert registry.get_tool_definition("probe") is not None

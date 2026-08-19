"""阶段 1.5：``ModelNotConfiguredError`` + resolver ``None`` 早返回测试。

覆盖目标（设计文档阶段 1.5 验收）：
- ``ModelNotConfiguredError`` 携带 ``error_code`` 属性（与 ``ErrorKind`` 枚举对齐）；
- ``ModelResolverService.resolve`` 在 ``requested_model=None`` 时抛
  ``ModelNotConfiguredError`` 且 reason 为 ``REASON_MODEL_NOT_SELECTED``；
- 早返回路径不透传 None 给 ``find_enabled_by_name``（避免 SQL 层错误）；
- ``_GUIDANCE_BY_REASON`` 含 ``REASON_MODEL_NOT_SELECTED`` 修复指引文案。
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from app.models.enums.error_kind import ErrorKind
from app.service.llm.model_resolver_service import (
    REASON_API_KEY_MISSING,
    REASON_MODEL_NOT_FOUND,
    REASON_MODEL_NOT_SELECTED,
    REASON_PROVIDER_DISABLED,
    ModelNotConfiguredError,
    ModelResolverService,
)


def _build_service_with_mocks() -> ModelResolverService:
    """构造注入 mock CRUD / ProviderService 的解析服务实例（不依赖 DB）。

    参数:
        无。

    返回:
        绑定 mock 依赖的 ``ModelResolverService`` 实例。

    异常:
        无。

    副作用:
        无（绕过 ``__init__`` 的 service_depends 装配，直接注入 mock）。
    """

    service = ModelResolverService.__new__(ModelResolverService)
    service._model_entry_crud = MagicMock()  # type: ignore[attr-defined]
    service._provider_service = MagicMock()  # type: ignore[attr-defined]
    return service


def test_model_not_configured_error_carries_error_code() -> None:
    """``ModelNotConfiguredError`` 实例携带 ``error_code`` 属性（与 ErrorKind 对齐）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 ``error_code`` 不等于 ``ErrorKind.MODEL_NOT_CONFIGURED.value``
            时由 pytest 抛出。

    副作用:
        无。
    """

    exc = ModelNotConfiguredError("foo", REASON_MODEL_NOT_FOUND)
    assert exc.error_code == ErrorKind.MODEL_NOT_CONFIGURED.value
    assert exc.error_code == "model_not_configured"


def test_reason_model_not_selected_constant_value() -> None:
    """``REASON_MODEL_NOT_SELECTED`` 短码值为 ``"model_not_selected"``。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当短码值偏离约定时由 pytest 抛出。

    副作用:
        无。
    """

    assert REASON_MODEL_NOT_SELECTED == "model_not_selected"
    # 既有短码不应受影响。
    assert REASON_MODEL_NOT_FOUND == "model_not_found"
    assert REASON_PROVIDER_DISABLED == "provider_disabled"
    assert REASON_API_KEY_MISSING == "api_key_missing"


def test_resolve_rejects_none_requested_model_early() -> None:
    """``resolve(requested_model=None)`` 早返回抛 ``REASON_MODEL_NOT_SELECTED``。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当未抛出 / reason 错误 / 仍调用 ``find_enabled_by_name``
            时由 pytest 抛出。

    副作用:
        无（mock 依赖，未读 DB）。
    """

    service = _build_service_with_mocks()
    with pytest.raises(ModelNotConfiguredError) as exc_info:
        service.resolve(requested_model=None)
    assert exc_info.value.reason == REASON_MODEL_NOT_SELECTED
    assert exc_info.value.model_name == "(none)"
    # 关键：None 透传前即被拦截，CRUD 不应被调用（避免 SQL 层 None 比较异常）。
    service._model_entry_crud.find_enabled_by_name.assert_not_called()  # type: ignore[attr-defined]


def test_resolve_none_rejection_carries_error_code() -> None:
    """``None`` 拒绝路径的异常携带 ``error_code`` 字段供 RunFailedPayload 透传。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当异常不携带 ``error_code`` 时由 pytest 抛出。

    副作用:
        无。
    """

    service = _build_service_with_mocks()
    with pytest.raises(ModelNotConfiguredError) as exc_info:
        service.resolve(requested_model=None)
    assert exc_info.value.error_code == "model_not_configured"


def test_resolve_none_rejection_provides_user_guidance() -> None:
    """``None`` 拒绝路径的异常携带面向用户的修复指引文案。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 ``guidance`` 为空或未提及「选择模型」时由 pytest 抛出。

    副作用:
        无。
    """

    service = _build_service_with_mocks()
    with pytest.raises(ModelNotConfiguredError) as exc_info:
        service.resolve(requested_model=None)
    guidance = cast(str, exc_info.value.guidance)
    assert guidance
    assert "选择模型" in guidance


def test_resolve_none_rejection_writes_warn_log(caplog) -> None:
    """``None`` 拒绝路径写 warn 级 ``model_resolve_rejected`` 日志（可排查）。

    参数:
        caplog: pytest 内置日志捕获 fixture。

    返回:
        无。

    异常:
        AssertionError: 当未写 warn 级日志 / event 键不匹配时由 pytest 抛出。

    副作用:
        捕获本测试范围内的 log 记录。
    """

    import logging

    caplog.set_level(logging.WARNING, logger="app")

    service = _build_service_with_mocks()
    with pytest.raises(ModelNotConfiguredError):
        service.resolve(requested_model=None)

    rejected_logs = [
        record for record in caplog.records if record.getMessage() == "model_resolve_rejected"
    ]
    assert len(rejected_logs) == 1
    log_record = rejected_logs[0]
    assert log_record.__dict__["data"]["reason"] == REASON_MODEL_NOT_SELECTED
    assert log_record.__dict__["data"]["model"] is None

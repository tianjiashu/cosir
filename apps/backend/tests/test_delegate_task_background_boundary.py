"""delegate_task background 字段边界核查测试（独立验证，不修改业务代码）。

仅验证 DelegateTaskArgs 与 DelegationExecutor._build_agent_input_text 的
background 边界行为，作为本次新增字段的边界补充证据。
"""

import logging

import pytest

from app.core.delegation.delegation_executor import DelegationExecutor
from app.tools.tool_models.delegate_task_args import (
    BACKGROUND_MAX,
    DelegateTaskArgs,
)


def _valid_kwargs(**overrides):
    base = {
        "child_agent_id": "delegate_reviewer",
        "title": "Review diff",
        "objective": "review the diff",
        "rules": ["do not modify files"],
        "references": ["src/main.py"],
        "expected_output": "review comments",
    }
    base.update(overrides)
    return base


def _executor() -> DelegationExecutor:
    """构造最小 DelegationExecutor 实例以调用实例方法 _build_agent_input_text。

    该拼装方法是纯函数式（仅依赖 args），不触及任何协作服务，故用占位依赖即可。
    """
    return DelegationExecutor(
        child_runner=None,  # type: ignore[arg-type]
        parent_profile=None,  # type: ignore[arg-type]
        parent_turn=None,  # type: ignore[arg-type]
        parent_task=None,  # type: ignore[arg-type]
    )


def test_build_agent_input_text_omits_background_when_empty():
    """边界核查：background 默认空时拼装文本不含 '## Background' section。

    目的：确认空 background 被省略（不输出空标题），可能暴露的缺陷：
    空字符串仍渲染 Background 空段导致子输入噪声。
    """
    args = DelegateTaskArgs.model_validate(_valid_kwargs())
    assert args.background == ""

    text = _executor()._build_agent_input_text(args)
    assert "## Background" not in text


def test_build_agent_input_text_omits_background_when_explicit_empty_string():
    """边界核查：显式 background='' 同样省略 Background section。

    目的：确认空白显式传参与默认空一致；可能暴露缺陷：显式空串仍渲染。
    """
    args = DelegateTaskArgs.model_validate(_valid_kwargs(background=""))
    text = _executor()._build_agent_input_text(args)
    assert "## Background" not in text


def test_background_over_budget_logs_event_key_without_secret(caplog):
    """边界核查：background 超预算时 log.warning 事件键为 delegate_task_args_over_budget。

    目的：确认超预算路径事件键正确且 extra.data 不含任何 secret/敏感字段；
    可能暴露的缺陷：事件键错误或 data 泄露父 agent 凭据/上下文。
    """
    with caplog.at_level(logging.WARNING, logger="coding_agent.backend"):
        with pytest.raises(Exception):
            DelegateTaskArgs.model_validate(
                _valid_kwargs(background="x" * (BACKGROUND_MAX + 1))
            )

    records = [
        r
        for r in caplog.records
        if r.getMessage() == "delegate_task_args_over_budget"
    ]
    assert records, "超预算时未以 delegate_task_args_over_budget 事件键写日志"

    record = records[0]
    assert getattr(record, "display_message", "") == "delegate_task background 超出预算上限"
    assert record.data["field"] == "background"
    assert record.data["limit"] == BACKGROUND_MAX
    assert record.data["actual"] == BACKGROUND_MAX + 1

    # 无 secret 泄露：data 仅含预算字段，不出现任何敏感键
    sensitive_keys = {"secret", "token", "api_key", "password", "credential", "key"}
    assert not (set(record.data.keys()) & sensitive_keys)

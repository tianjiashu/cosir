"""delegation policy tests."""

import pytest

from app.service.delegation.delegation_context import DelegationPolicyContext
from app.service.delegation.delegation_policy import DelegationPolicy


def test_effective_tools_are_intersection():
    """验证有效工具为 parent、child、system 三方权限交集。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当交集结果不符合预期时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = DelegationPolicyContext(
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        parent_allowed_tools=frozenset({"read_file", "write_file", "delegate_task"}),
        child_allowed_tools=frozenset({"read_file", "search_files"}),
        system_allowed_tools=frozenset({"read_file", "search_files", "write_file"}),
        depth=0,
        max_depth=1,
        running_children=0,
        max_concurrency=1,
        known_child_agent_ids=frozenset(
            {"delegate_reviewer", "delegate_analyst", "delegate_coder"}
        ),
    )

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is True
    assert decision.effective_tools == ("read_file",)


@pytest.mark.parametrize("depth,running_children", [(1, 0), (0, 1)])
def test_policy_rejects_depth_or_concurrency(depth, running_children):
    """验证深度或并发达到上限时会拒绝委派。

    参数:
        depth: 当前委派深度。
        running_children: 当前运行中的 child 数量。

    返回:
        无。

    异常:
        AssertionError: 当策略未拒绝时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = DelegationPolicyContext(
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        parent_allowed_tools=frozenset({"read_file", "delegate_task"}),
        child_allowed_tools=frozenset({"read_file"}),
        system_allowed_tools=frozenset({"read_file"}),
        depth=depth,
        max_depth=1,
        running_children=running_children,
        max_concurrency=1,
        known_child_agent_ids=frozenset({"delegate_reviewer"}),
    )

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is False
    assert decision.reason


def test_policy_context_defaults_to_v1_limits():
    """验证策略上下文使用 v1 的默认深度和并发上限。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当默认值不是 1 时由 pytest 抛出。

    副作用:
        构造未显式指定限制的本地策略上下文。
    """

    context = DelegationPolicyContext(
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        parent_allowed_tools=frozenset({"read_file", "delegate_task"}),
        child_allowed_tools=frozenset({"read_file"}),
        system_allowed_tools=frozenset({"read_file"}),
        depth=0,
        running_children=0,
        known_child_agent_ids=frozenset({"delegate_reviewer"}),
    )

    assert context.max_depth == 1
    assert context.max_concurrency == 1


def test_effective_tools_are_sorted_deterministically():
    """验证多工具交集结果按字典序排序，保证跨运行确定性。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当交集结果顺序不确定时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = DelegationPolicyContext(
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        parent_allowed_tools=frozenset({"search_files", "read_file", "delegate_task"}),
        child_allowed_tools=frozenset({"search_files", "read_file"}),
        system_allowed_tools=frozenset({"search_files", "read_file"}),
        depth=0,
        max_depth=1,
        running_children=0,
        max_concurrency=1,
        known_child_agent_ids=frozenset({"delegate_reviewer"}),
    )

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is True
    assert decision.effective_tools == ("read_file", "search_files")


def test_policy_rejects_unknown_child_agent():
    """验证未知 child agent 返回固定拒绝原因。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当拒绝原因不是 unknown_child_agent 时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = DelegationPolicyContext(
        parent_agent_id="developer",
        child_agent_id="unknown_child",
        parent_allowed_tools=frozenset({"read_file", "delegate_task"}),
        child_allowed_tools=frozenset({"read_file"}),
        system_allowed_tools=frozenset({"read_file"}),
        depth=0,
        max_depth=1,
        running_children=0,
        max_concurrency=1,
        known_child_agent_ids=frozenset({"delegate_reviewer"}),
    )

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is False
    assert decision.reason == "unknown_child_agent"


def test_policy_rejects_when_three_way_intersection_is_empty():
    """验证三方交集为空时返回固定拒绝原因。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当拒绝原因不是 no_effective_tools 时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = DelegationPolicyContext(
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        parent_allowed_tools=frozenset({"write_file", "delegate_task"}),
        child_allowed_tools=frozenset({"read_file"}),
        system_allowed_tools=frozenset({"write_file", "read_file"}),
        depth=0,
        max_depth=1,
        running_children=0,
        max_concurrency=1,
        known_child_agent_ids=frozenset({"delegate_reviewer"}),
    )

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is False
    assert decision.reason == "no_effective_tools"

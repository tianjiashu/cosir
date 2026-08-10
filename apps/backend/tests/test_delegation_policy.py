"""delegation policy tests."""

import pytest

from app.service.delegation.delegation_context import DelegationPolicyContext
from app.service.delegation.delegation_policy import DelegationPolicy


def test_effective_tools_are_intersection():
    context = DelegationPolicyContext(
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        requested_tools=("read_file", "write_file", "execute_terminal"),
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
    context = DelegationPolicyContext(
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        requested_tools=("read_file",),
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

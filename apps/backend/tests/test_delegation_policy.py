"""delegation policy tests."""

from app.service.delegation.delegation_context import DelegationPolicyContext
from app.service.delegation.delegation_policy import DelegationPolicy


def _make_context(
        *,
        child_agent_id: str = "delegate_reviewer",
        child_allowed_tools: frozenset[str] = frozenset({"read_file"}),
        depth: int = 0,
        known_child_agent_ids: frozenset[str] = frozenset({"delegate_reviewer"}),
) -> DelegationPolicyContext:
    """构造策略上下文的本地工厂，集中收口调用点避免重复。

    参数:
        child_agent_id: 目标 child Agent 标识。
        child_allowed_tools: child Agent 可用工具集合。
        depth: 当前委派链深度。
        known_child_agent_ids: 已知 child Agent 标识集合。

    返回:
        一个 ``DelegationPolicyContext`` 实例（``max_depth`` 取默认 1）。

    异常:
        无。

    副作用:
        无。
    """

    return DelegationPolicyContext(
        parent_agent_id="developer",
        child_agent_id=child_agent_id,
        child_allowed_tools=child_allowed_tools,
        depth=depth,
        known_child_agent_ids=known_child_agent_ids,
    )


def test_effective_tools_are_child_set_minus_delegate_task():
    """验证有效工具为 child 工具集合剔除 delegate_task 后的结果。

    委派策略不再引入父 Agent 与系统级工具集合，child 声明中的 ``delegate_task``
    会在处理过程中被剔除，避免 child 获得递归委派能力。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当有效工具不符合预期时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = _make_context(
        child_allowed_tools=frozenset({"read_file", "write_file", "delegate_task"}),
    )

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is True
    assert decision.effective_tools == ("read_file", "write_file")


def test_policy_strips_delegate_task_to_prevent_recursion():
    """验证 child 单独声明 delegate_task 时被剔除、不产生递归委派。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 delegate_task 未被剔除或错误放行时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = _make_context(
        child_allowed_tools=frozenset({"delegate_task"}),
    )

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is False
    assert decision.reason == "no_effective_tools"


def test_policy_rejects_when_depth_reached():
    """验证委派深度达到上限时会拒绝委派。

    并发额度（max_concurrency）的裁决已下沉到 storage 层原子 acquire，不再由策略层
    校验，因此本用例只覆盖深度维度。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当策略未拒绝时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = _make_context(depth=1)

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is False
    assert decision.reason == "delegation_depth_exceeded"


def test_policy_context_defaults_to_v1_limits():
    """验证策略上下文使用 v1 的默认深度。

    并发上限（max_concurrency）已不在策略上下文中，由 executor 从
    ``Settings.DELEGATION_MAX_CONCURRENCY`` 读取后传入 storage 层。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当默认值不是 1 时由 pytest 抛出。

    副作用:
        构造未显式指定限制的本地策略上下文。
    """

    context = _make_context()

    assert context.max_depth == 1


def test_effective_tools_are_sorted_deterministically():
    """验证有效工具结果按字典序排序，保证跨运行确定性。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当结果顺序不确定时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = _make_context(
        child_allowed_tools=frozenset({"search_files", "read_file", "delegate_task"}),
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

    context = _make_context(child_agent_id="unknown_child")

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is False
    assert decision.reason == "unknown_child_agent"


def test_policy_rejects_when_child_tools_empty_after_strip():
    """验证 child 工具集合剔除 delegate_task 后为空时返回固定拒绝原因。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当拒绝原因不是 no_effective_tools 时由 pytest 抛出。

    副作用:
        构造本地策略上下文并调用策略解析。
    """

    context = _make_context(child_allowed_tools=frozenset())

    decision = DelegationPolicy().resolve(context)

    assert decision.allowed is False
    assert decision.reason == "no_effective_tools"

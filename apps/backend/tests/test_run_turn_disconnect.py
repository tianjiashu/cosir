"""``AgentRuntime`` 断开兜底与认领竞态的回归测试。

覆盖本次修复的核心不变量：
- 落败认领（``claim_pending_turn`` 返回 ``False``）的连接**绝不**改动 turn 状态，
  从而不会误标他连接正在驱动的 ``running`` turn（竞态误杀修复）。
- ``_mark_turn_disconnected_if_running`` 仅对仍处于 ``running`` 的 turn 落定为
  ``failed``（``client_disconnected``），对已终态 turn 为幂等空操作。
"""

from app.core.runtime.runner import AgentRuntime


class _FakeTurn:
    """测试用轻量轮次记录替身。"""

    def __init__(
        self,
        status: str = "pending",
        agent_id: str = "dev",
        turn_id: str = "turn-1",
        task_id: str = "task-1",
    ) -> None:
        """初始化替身轮次。

        参数:
            status: 轮次状态。
            agent_id: 绑定的 agent 标识。
            turn_id: 轮次标识。
            task_id: 所属任务标识。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self.status = status
        self.agent_id = agent_id
        self.turn_id = turn_id
        self.task_id = task_id


class _FakeTask:
    """测试用轻量任务记录替身。"""

    def __init__(self, agent_id: str = "dev", task_id: str = "task-1") -> None:
        """初始化替身任务。

        参数:
            agent_id: 任务默认归属 agent 标识。
            task_id: 任务标识。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self.agent_id = agent_id
        self.task_id = task_id


class _RecordingTurnService:
    """记录状态变更调用的轮次 service 替身。"""

    def __init__(self, turn: _FakeTurn, claim_result: bool) -> None:
        """初始化替身 service。

        参数:
            turn: 受管理的替身轮次。
            claim_result: ``claim_pending_turn`` 的固定返回值（模拟认领成功/落败）。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self._turn = turn
        self._claim_result = claim_result
        self.status_updates: list[tuple[str, str, str | None]] = []
        self._statuses: dict[str, str] = {turn.turn_id: turn.status}

    def get_turn(self, turn_id: str) -> _FakeTurn:
        """返回受管理的替身轮次。"""

        return self._turn

    def claim_pending_turn(self, turn_id: str) -> bool:
        """返回预设的认领结果。"""

        return self._claim_result

    def update_turn_status(
        self, turn_id: str, status: str, end_reason: str | None = None
    ) -> _FakeTurn:
        """记录一次状态变更并更新替身状态。"""

        self.status_updates.append((turn_id, status, end_reason))
        self._statuses[turn_id] = status
        self._turn.status = status
        return self._turn

    def has_turn_status(self, turn_id: str, status: str) -> bool:
        """返回替身轮次当前是否处于给定状态。"""

        return self._statuses.get(turn_id) == status


class _FakeTaskService:
    """测试用任务 service 替身。"""

    def __init__(self, task: _FakeTask) -> None:
        """记录受管理的替身任务。"""

        self._task = task

    def get_task(self, task_id: str) -> _FakeTask:
        """返回受管理的替身任务。"""

        return self._task


class _FakeRegistry:
    """测试用 agent profile 目录替身。"""

    def __init__(self, profile: object) -> None:
        """记录固定解析出的 profile。"""

        self._profile = profile

    def resolve(self, agent_id: str) -> object:
        """始终返回预设 profile（非 None，模拟解析成功）。"""

        return self._profile


def _build_runtime(turn_service: _RecordingTurnService) -> AgentRuntime:
    """用替身协作者装配一个仅供本测试使用的 ``AgentRuntime``。

    参数:
        turn_service: 记录型轮次 service 替身。

    返回:
        装配好的 ``AgentRuntime`` 实例（未触达重依赖构造）。

    异常:
        无。

    副作用:
        无。
    """

    return AgentRuntime(
        settings=None,  # type: ignore[arg-type]
        task_service=_FakeTaskService(_FakeTask()),  # type: ignore[arg-type]
        turn_service=turn_service,  # type: ignore[arg-type]
        context_builder=None,  # type: ignore[arg-type]
        tool_scheduler=None,  # type: ignore[arg-type]
        agent_registry=_FakeRegistry(object()),  # type: ignore[arg-type]
    )


async def test_run_turn_claim_lost_does_not_touch_status() -> None:
    """落败认领的连接不得改动 turn 状态（竞态误杀回归）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当落败连接产出了事件或改动了 turn 状态时。

    副作用:
        无（仅写测试日志）。
    """

    turn = _FakeTurn(status="pending")
    turn_service = _RecordingTurnService(turn, claim_result=False)
    runtime = _build_runtime(turn_service)

    events = [event async for event in runtime.run_turn(turn.turn_id, turn=turn)]

    assert events == []
    assert turn_service.status_updates == []


def test_mark_disconnected_marks_running_turn_failed() -> None:
    """仍处于 running 的持有轮次应被标记为断开失败。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当未按预期落定为 ``failed`` / ``client_disconnected`` 时。

    副作用:
        无。
    """

    turn = _FakeTurn(status="running")
    turn_service = _RecordingTurnService(turn, claim_result=True)
    runtime = _build_runtime(turn_service)

    runtime._mark_turn_disconnected_if_running(turn.turn_id)

    assert turn_service.status_updates == [(turn.turn_id, "failed", "client_disconnected")]


def test_mark_disconnected_noop_for_terminal_turn() -> None:
    """已终态（completed）的轮次不应被断开兜底改动。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当对已完成轮次产生了状态变更时。

    副作用:
        无。
    """

    turn = _FakeTurn(status="completed")
    turn_service = _RecordingTurnService(turn, claim_result=True)
    runtime = _build_runtime(turn_service)

    runtime._mark_turn_disconnected_if_running(turn.turn_id)

    assert turn_service.status_updates == []

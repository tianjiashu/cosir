"""``AgentProfileRegistry`` 并发契约回归测试。

覆盖两条契约：

1. 写操作（``register`` / ``replace`` / ``unregister``）的「判定 + 提交」在同一临界区内完成，
   因此同一 key 被多线程同时注册时只允许一个线程成功；
2. 读操作在同一临界区内读取索引，与并发写入交错时既不会抛
   ``RuntimeError: dictionary changed size during iteration``，也不会读到半装填状态。

不覆盖配置目录读取、作用域隔离与冲突裁决规则（见 ``test_agent_profile_config.py`` 与
``test_system_prompt_tool_layer.py``）。
"""

from __future__ import annotations

import threading

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry

_SCOPE = AgentProfileRegistry.SYSTEM_WORKSPACE
_WRITER_ROUNDS = 2000
_JOIN_TIMEOUT = 10.0


def _profile(agent_id: str) -> AgentProfile:
    """构造一个满足注册契约的 CHILD profile（``workflow=None`` 跳过工作流装配）。"""

    return AgentProfile(
        agent_id=agent_id,
        role=agent_id,
        description=f"concurrency probe {agent_id}",
        allowed_tools=["read_file"],
        agent_type=AgentProfileType.CHILD,
        system_prompt="probe",
        workflow=None,  # type: ignore[arg-type]
    )


def test_concurrent_register_of_same_key_accepts_exactly_one() -> None:
    """同一 key 被多线程同时注册时，只有一个线程能成功。"""

    registry = AgentProfileRegistry()
    profile = _profile("duplicated")
    barrier = threading.Barrier(8)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=_JOIN_TIMEOUT)
        accepted = registry.register(_SCOPE, profile)
        with results_lock:
            results.append(accepted)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT)

    assert len(results) == 8
    assert results.count(True) == 1
    assert registry.resolve(_SCOPE, "duplicated") is not None


def test_reads_are_safe_while_writes_are_in_flight() -> None:
    """注册/卸载交错进行时，``list`` 与 ``resolve`` 不抛异常且只看到完整快照。"""

    registry = AgentProfileRegistry()
    done = threading.Event()
    failures: list[BaseException] = []

    def writer(prefix: str, start: threading.Barrier) -> None:
        start.wait(timeout=_JOIN_TIMEOUT)
        for index in range(_WRITER_ROUNDS):
            agent_id = f"{prefix}-{index}"
            registry.register(_SCOPE, _profile(agent_id))
            registry.unregister(_SCOPE, agent_id)

    def reader(start: threading.Barrier) -> None:
        start.wait(timeout=_JOIN_TIMEOUT)
        try:
            while not done.is_set():
                listed = registry.list(_SCOPE)
                assert all(item.agent_id for item in listed)
                registry.resolve(_SCOPE, "absent")
        except BaseException as exc:  # 任何异常（含迭代 RuntimeError）都判失败
            failures.append(exc)
            done.set()

    writer_count = 2
    reader_count = 3
    start = threading.Barrier(writer_count + reader_count)
    writers = [
        threading.Thread(target=writer, args=(f"w{index}", start))
        for index in range(writer_count)
    ]
    readers = [threading.Thread(target=reader, args=(start,)) for _ in range(reader_count)]
    for thread in [*writers, *readers]:
        thread.start()
    for thread in writers:
        thread.join(timeout=_JOIN_TIMEOUT)
    done.set()
    for thread in readers:
        thread.join(timeout=_JOIN_TIMEOUT)

    assert not failures
    # 每个 writer 都是「注册后立即卸载」，写完卸载后索引应回到空状态。
    assert registry.list(_SCOPE) == []

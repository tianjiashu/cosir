"""``AgentProfileRegistry`` 并发契约回归测试。

覆盖两条契约：

1. 并发读到同一个配置变化时只重读一次（装载锁内二次比对签名）；
2. 配置在读取期间不断变化时，``list`` / ``resolve`` 既不抛异常，也不读到半装填状态（快照整体替换）。

不覆盖配置目录读取、作用域隔离与冲突裁决规则（见 ``test_agent_profile_config.py`` 与
``test_lazy_loading_adversarial.py``）。
"""

from __future__ import annotations

import threading

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from tests.static_agent_source import StaticAgentSource

_SCOPE = AgentProfileRegistry.SYSTEM_WORKSPACE
_WRITER_ROUNDS = 2000
_JOIN_TIMEOUT = 10.0


def _profile(agent_id: str) -> AgentProfile:
    """构造一个 CHILD profile（``workflow=None`` 跳过工作流装配）。"""

    return AgentProfile(
        agent_id=agent_id,
        role=agent_id,
        description=f"concurrency probe {agent_id}",
        allowed_tools=["read_file"],
        agent_type=AgentProfileType.CHILD,
        system_prompt="probe",
    )


def test_concurrent_reads_of_one_change_load_once() -> None:
    loads: list[str] = []
    lock = threading.Lock()

    class _CountingSource(StaticAgentSource):
        def load(self, scope: str) -> list[AgentProfile]:
            with lock:
                loads.append(scope)
            return super().load(scope)

    registry = AgentProfileRegistry(_CountingSource({_SCOPE: [_profile("only")]}))
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait(timeout=_JOIN_TIMEOUT)
        assert registry.resolve(_SCOPE, "only") is not None

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT)

    assert loads == [_SCOPE]


def test_reads_are_safe_while_configuration_changes() -> None:
    """配置不断增删时，``list`` 与 ``resolve`` 不抛异常且只看到完整快照。"""

    source = StaticAgentSource({_SCOPE: []})
    registry = AgentProfileRegistry(source)
    done = threading.Event()
    failures: list[BaseException] = []

    def writer(prefix: str, start: threading.Barrier) -> None:
        start.wait(timeout=_JOIN_TIMEOUT)
        for index in range(_WRITER_ROUNDS):
            # 整体换掉列表：与真实来源「每次读到的是一整份文件」一致。
            source.profiles[_SCOPE] = [_profile(f"{prefix}-{index}")]
            source.profiles[_SCOPE] = []

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
    assert registry.list(_SCOPE) == []

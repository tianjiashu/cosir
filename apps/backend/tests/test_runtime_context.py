"""``RuntimeContext`` 单元测试。

聚焦 task 级上下文管理自身行为：纯初始化无 I/O、线程安全读写、历史加载、
``with`` 生命周期、一致性快照，以及 task 隔离与 subAgent 铺垫（create_child）、
压缩预留接口（maybe_compact）。外部依赖（service CRUD / SystemPromptBuilder）
以 mock 隔离，避免与持久化链路或提示构建耦合。
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.agents.agent_profile import AgentProfile
from app.core.context.runtime_context import RuntimeContext

# 测试用固定工作区根路径（非真实临时目录，仅作字符串占位避免 S108）。
MOCK_WORKSPACE = "mock_workspace_root"


def _fake_agent_profile() -> AgentProfile:
    """返回最小合法 agent 画像，供构造测试使用（避免触发 workflow 延迟导入）。"""
    return AgentProfile(
        agent_id="dev",
        role="coding-agent",
        goal="协助用户完成软件工程项目开发任务",
        allowed_tools=[],
        context_policy="text_only_v1",
    )


def _patch_system_prompt(test_case: unittest.TestCase) -> None:
    """隔离 ``RuntimeContext._build_system_message``，返回固定系统消息。

    直接 patch 方法避免依赖 ``SystemPromptBuilder.build`` 的 mock 机制与
    langchain 反序列化副作用，聚焦 RuntimeContext 自身行为。

    参数:
        test_case: 当前测试用例，用于注册清理。

    返回:
        无。
    """
    patcher = mock.patch.object(
        RuntimeContext,
        "_build_system_message",
        return_value=SystemMessage(content="sys"),
    )
    patcher.start()
    test_case.addCleanup(patcher.stop)


class TestRuntimeContextInit(unittest.TestCase):
    """构造与纯初始化行为（不应触发任何 I/O）。"""

    def test_init_has_no_io_and_seeds_system_message(self) -> None:
        """构造不应查询数据库，且仅含系统提示一条消息。"""
        _patch_system_prompt(self)
        ctx = RuntimeContext(
            agent_profile=_fake_agent_profile(),
            workspace_root=MOCK_WORKSPACE,
            task_id="task-1",
        )
        self.assertEqual(len(ctx.messages), 1)
        self.assertIsInstance(ctx.messages[0], SystemMessage)
        self.assertTrue(ctx.os_name)
        self.assertEqual(len(ctx.today), 10)  # YYYY-MM-DD

    def test_init_does_not_touch_service_crud(self) -> None:
        """构造期间 TurnService 门面不得被调用（I/O 隔离）。"""
        _patch_system_prompt(self)
        with mock.patch(
            "app.core.context.runtime_context.TurnService"
        ) as turn_service_cls:
            RuntimeContext(
                agent_profile=_fake_agent_profile(),
                workspace_root=MOCK_WORKSPACE,
                task_id="task-1",
            )
        turn_service_cls.assert_not_called()


class TestRuntimeContextThreadSafety(unittest.TestCase):
    """线程安全的追加与读取接口。"""

    def setUp(self) -> None:
        _patch_system_prompt(self)
        self.ctx = RuntimeContext(
            agent_profile=_fake_agent_profile(),
            workspace_root=MOCK_WORKSPACE,
            task_id="task-1",
        )

    def test_add_message_appends_under_lock(self) -> None:
        """add_message 在锁保护下追加消息。"""
        self.ctx.add_message(HumanMessage(content="hi"))
        self.assertEqual(len(self.ctx.messages), 2)
        self.assertIsInstance(self.ctx.messages[1], HumanMessage)

    def test_load_message_returns_copy_not_reference(self) -> None:
        """load_message 返回拷贝，修改拷贝不影响内部状态。"""
        self.ctx.add_message(HumanMessage(content="hi"))
        snapshot = self.ctx.load_message()
        snapshot.append(AIMessage(content="injected"))
        self.assertEqual(len(self.ctx.messages), 2)

    def test_snapshot_is_independent(self) -> None:
        """snapshot 返回独立拷贝。"""
        self.ctx.add_message(HumanMessage(content="hi"))
        snap = self.ctx.snapshot()
        snap.clear()
        self.assertEqual(len(self.ctx.messages), 2)

    def test_concurrent_add_message_no_loss(self) -> None:
        """多线程并发追加不丢消息。"""
        def producer() -> None:
            for i in range(50):
                self.ctx.add_message(HumanMessage(content=f"m{i}"))

        threads = [threading.Thread(target=producer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.ctx.messages), 1 + 200)


class TestRuntimeContextLoadForTask(unittest.TestCase):
    """显式历史加载入口。"""

    def test_load_for_task_extends_history(self) -> None:
        """load_for_task 应经 TurnService 门面查询并把历史消息转换后追加。"""
        _patch_system_prompt(self)
        from app.models import RuntimeMessage

        turn_service = mock.MagicMock()
        turn_service.list_turns_for_task.return_value = [
            mock.MagicMock(turn_id="turn-1")
        ]
        turn_service.load_turn_messages.return_value = [
            RuntimeMessage(role="user", content_text="hello"),
            RuntimeMessage(
                role="assistant",
                content_text="let me",
                metadata={"tool_calls": '[{"name": "read_file", "args": {"p": "a"}, "id": "c1"}]'},
            ),
            RuntimeMessage(
                role="tool",
                content_text="result",
                metadata={"tool_call_id": "c1"},
            ),
        ]
        with mock.patch(
            "app.core.context.runtime_context.TurnService",
            return_value=turn_service,
        ):
            ctx = RuntimeContext.load_for_task(
                agent_profile=_fake_agent_profile(),
                workspace_root=MOCK_WORKSPACE,
                task_id="task-1",
            )
        self.assertEqual(len(ctx.messages), 4)
        self.assertIsInstance(ctx.messages[1], HumanMessage)
        self.assertIsInstance(ctx.messages[2], AIMessage)
        self.assertEqual(ctx.messages[2].tool_calls[0]["name"], "read_file")
        turn_service.list_turns_for_task.assert_called_once_with("task-1")


class TestRuntimeContextWithProtocol(unittest.TestCase):
    """with 生命周期管理。"""

    def test_enter_exit_lifecycle(self) -> None:
        """__enter__ 返回自身，__exit__ 正常落幕且不抛异常。"""
        _patch_system_prompt(self)
        turn_service = mock.MagicMock()
        turn_service.list_turns_for_task.return_value = []
        with mock.patch(
            "app.core.context.runtime_context.TurnService",
            return_value=turn_service,
        ):
            ctx = RuntimeContext.load_for_task(
                agent_profile=_fake_agent_profile(),
                workspace_root=MOCK_WORKSPACE,
                task_id="task-1",
            )
        with ctx as entered:
            self.assertIs(entered, ctx)
            entered.add_message(HumanMessage(content="x"))
        self.assertEqual(len(ctx.messages), 2)


class TestRuntimeContextTaskIsolation(unittest.TestCase):
    """task 隔离与 subAgent 铺垫（create_child）。"""

    def setUp(self) -> None:
        _patch_system_prompt(self)
        self.parent = RuntimeContext(
            task_id="parent-task",
            agent_profile=_fake_agent_profile(),
            workspace_root=MOCK_WORKSPACE,
        )
        self.parent.add_message(HumanMessage(content="parent-history"))

    def test_parent_context_default_none(self) -> None:
        """顶层 task 的 parent_context 为 None。"""
        self.assertIsNone(self.parent.parent_context)

    def test_create_child_inherits_system_and_history_snapshot(self) -> None:
        """子上下文继承父系统提示与历史只读快照。"""
        child = self.parent.create_child(task_id="child-task")
        # 1 系统（自带） + 1 父历史快照
        self.assertEqual(len(child.messages), 2)
        self.assertIsInstance(child.messages[0], SystemMessage)
        self.assertEqual(child.messages[1].content, "parent-history")

    def test_create_child_has_independent_messages(self) -> None:
        """子上下文消息列表独立，追加不影响父上下文（task 隔离）。"""
        child = self.parent.create_child(task_id="child-task")
        child.add_message(HumanMessage(content="child-only"))
        self.assertEqual(len(child.messages), 3)
        # 父上下文不受影响
        self.assertEqual(len(self.parent.messages), 2)

    def test_create_child_has_independent_lock(self) -> None:
        """子上下文拥有独立锁实例，不与父共享可变状态。"""
        child = self.parent.create_child(task_id="child-task")
        self.assertIsNot(child.lock, self.parent.lock)

    def test_create_child_links_parent(self) -> None:
        """子上下文可经 parent_context 反向追溯到父。"""
        child = self.parent.create_child(task_id="child-task")
        self.assertIs(child.parent_context, self.parent)
        self.assertEqual(child.parent_context.task_id, "parent-task")

    def test_create_child_defaults_profile_and_root(self) -> None:
        """create_child 缺省沿用父画像与工作区。"""
        child = self.parent.create_child(task_id="child-task")
        self.assertIs(child.agent_profile, self.parent.agent_profile)
        self.assertEqual(child.workspace_root, self.parent.workspace_root)


class TestRuntimeContextCompressionReserve(unittest.TestCase):
    """压缩预留接口（ContextCompressor 协议 / maybe_compact）。"""

    def setUp(self) -> None:
        _patch_system_prompt(self)
        self.ctx = RuntimeContext(
            task_id="task-1",
            agent_profile=_fake_agent_profile(),
            workspace_root=MOCK_WORKSPACE,
        )

    def test_maybe_compact_noop_without_compressor(self) -> None:
        """未配置压缩器时 maybe_compact 返回 False 且不改动消息。"""
        self.ctx.add_message(HumanMessage(content="hi"))
        result = self.ctx.maybe_compact()
        self.assertFalse(result)
        self.assertEqual(len(self.ctx.messages), 2)

    def test_maybe_compact_invokes_compressor(self) -> None:
        """配置压缩器时 maybe_compact 调用协议并替换消息。"""
        compressor = mock.MagicMock()
        compressor.compact.return_value = [
            SystemMessage(content="sys"),
            HumanMessage(content="compressed"),
        ]
        self.ctx.compressor = compressor  # type: ignore[assignment]
        self.ctx.add_message(HumanMessage(content="hi"))
        result = self.ctx.maybe_compact()
        self.assertTrue(result)
        compressor.compact.assert_called_once()
        # 压缩后消息被替换为压缩器返回值
        self.assertEqual(len(self.ctx.messages), 2)
        self.assertEqual(self.ctx.messages[1].content, "compressed")


class TestRuntimeContextPostInit(unittest.TestCase):
    """__post_init__ 系统提示构建与去重。"""

    def test_post_init_seeds_system_message_once(self) -> None:
        """构造后恰好一条系统提示位于首位。"""
        _patch_system_prompt(self)
        ctx = RuntimeContext(
            task_id="task-1",
            agent_profile=_fake_agent_profile(),
            workspace_root=MOCK_WORKSPACE,
        )
        self.assertEqual(len(ctx.messages), 1)
        self.assertIsInstance(ctx.messages[0], SystemMessage)

    def test_post_init_skips_when_system_already_present(self) -> None:
        """预置系统提示时不重复追加（create_child 路径安全）。"""
        _patch_system_prompt(self)
        ctx = RuntimeContext(
            task_id="task-1",
            agent_profile=_fake_agent_profile(),
            workspace_root=MOCK_WORKSPACE,
            messages=[SystemMessage(content="preset")],
        )
        # 不重复追加，保持预置的一条
        self.assertEqual(len(ctx.messages), 1)


if __name__ == "__main__":
    unittest.main()

"""Durable Run State 基础能力测试。"""

from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.domain.approvals.service import ApprovalService
from app.domain.approvals.store import ApprovalStore
from app.domain.artifacts.files import ArtifactFileStore
from app.domain.artifacts.store import ArtifactStore
from app.domain.human_input.service import HumanInputService
from app.domain.human_input.store import HumanInputStore
from app.core.runs.recovery import RecoveryManager
from app.core.runs.checkpointer import ManagedSqliteCheckpointer
from app.core.runs.resume import ResumeDispatcher
from app.core.runs.state_machine import InvalidRunTransition, RunStateMachine
from app.core.runs.store import DurableRunStore
from app.tools.execution.policy import ToolExecutionPolicy
from app.tools.execution.service import ToolExecutionService
from app.tools.execution.store import ToolExecutionStore


class DurableRunStateTests(unittest.TestCase):
    """校验 Durable Run State 基础模块符合持久化和幂等要求。"""

    def test_state_machine_rejects_terminal_resume(self) -> None:
        """校验完成态不能继续恢复。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果非法状态流转未被拒绝。

        副作用:
            无。
        """

        machine = RunStateMachine()

        with self.assertRaises(InvalidRunTransition):
            machine.ensure_transition("completed", "resuming")

    def test_approval_decision_creates_resume_command(self) -> None:
        """校验审批决策会创建恢复命令且重复提交幂等。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果审批状态、恢复命令或幂等行为不符合预期。

        副作用:
            创建临时 SQLite 数据库并写入审批相关记录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            logger = _null_logger("approval")
            run_store = DurableRunStore(database_path, logger)
            dispatcher = ResumeDispatcher(run_store, logger)
            service = ApprovalService(
                approval_store=ApprovalStore(database_path),
                run_store=run_store,
                resume_dispatcher=dispatcher,
                logger=logger,
            )
            run = run_store.create_for_task("task-1", thread_id="thread-1")

            approval = service.request_approval(
                run_id=run.run_id,
                tool_name="write_file",
                permission="write_file",
                risk_level="high",
                payload={"path": "note.txt"},
            )
            first = service.decide(approval.approval_id, "approved", "ok", "approval-key")
            second = service.decide(approval.approval_id, "approved", "ok", "approval-key")

            self.assertEqual(first.decision_id, second.decision_id)
            self.assertEqual(first.to_dict()["decision"], "approved")
            self.assertEqual(run_store.get(run.run_id).status, "resuming")
            self.assertIsNotNone(run_store.get_resume_command_by_key("resume:approval-key"))

    def test_approval_request_rolls_back_when_run_cannot_wait(self) -> None:
        """校验审批请求与运行等待状态在同一事务中提交。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果非法状态下留下孤儿审批请求。

        副作用:
            创建临时 SQLite 数据库并尝试写入审批请求。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            logger = _null_logger("approval-orphan")
            run_store = DurableRunStore(database_path, logger)
            service = ApprovalService(
                approval_store=ApprovalStore(database_path),
                run_store=run_store,
                resume_dispatcher=ResumeDispatcher(run_store, logger),
                logger=logger,
            )
            run = run_store.create_for_task("task-orphan", thread_id="thread-orphan")
            run_store.mark_status(run.run_id, "running")
            run_store.mark_status(run.run_id, "completed")

            with self.assertRaises(InvalidRunTransition):
                service.request_approval(
                    run_id=run.run_id,
                    tool_name="write_file",
                    permission="write_file",
                    risk_level="high",
                    payload={"path": "note.txt"},
                )

            self.assertEqual(service.list_pending(run.run_id), [])

    def test_approval_decision_reuses_existing_record_for_different_retry_key(self) -> None:
        """校验同一审批用不同幂等键重试时复用已有决策。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果重复提交触发新决策或重复恢复命令。

        副作用:
            创建临时 SQLite 数据库并写入审批决策与恢复命令。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            logger = _null_logger("approval-retry")
            run_store = DurableRunStore(database_path, logger)
            dispatcher = ResumeDispatcher(run_store, logger)
            service = ApprovalService(
                approval_store=ApprovalStore(database_path),
                run_store=run_store,
                resume_dispatcher=dispatcher,
                logger=logger,
            )
            run = run_store.create_for_task("task-retry", thread_id="thread-retry")
            approval = service.request_approval(
                run_id=run.run_id,
                tool_name="write_file",
                permission="write_file",
                risk_level="high",
                payload={"path": "note.txt"},
            )

            first = service.decide(approval.approval_id, "approved", "ok", "retry-key-1")
            second = service.decide(approval.approval_id, "approved", "ok", "retry-key-2")

            self.assertEqual(first.decision_id, second.decision_id)
            self.assertIsNotNone(run_store.get_resume_command_by_key("resume:retry-key-1"))
            self.assertIsNone(run_store.get_resume_command_by_key("resume:retry-key-2"))

    def test_concurrent_approval_decision_reuses_single_record(self) -> None:
        """校验并发审批决策不会因唯一键冲突失败。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果并发提交创建多条决策或抛出数据库唯一键异常。

        副作用:
            创建临时 SQLite 数据库并用两个线程提交同一审批决策。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            logger = _null_logger("approval-concurrent")
            run_store = DurableRunStore(database_path, logger)
            service = ApprovalService(
                approval_store=ApprovalStore(database_path),
                run_store=run_store,
                resume_dispatcher=ResumeDispatcher(run_store, logger),
                logger=logger,
            )
            run = run_store.create_for_task("task-concurrent-approval", thread_id="thread-concurrent-approval")
            approval = service.request_approval(
                run_id=run.run_id,
                tool_name="write_file",
                permission="write_file",
                risk_level="high",
                payload={"path": "note.txt"},
            )

            with ThreadPoolExecutor(max_workers=2) as executor:
                decisions = list(
                    executor.map(
                        lambda _: service.decide(approval.approval_id, "approved", "ok", "same-key"),
                        range(2),
                    )
                )

            self.assertEqual({item.decision_id for item in decisions}, {decisions[0].decision_id})
            self.assertIsNotNone(run_store.get_resume_command_by_key("resume:same-key"))

    def test_concurrent_resume_command_creation_reuses_single_record(self) -> None:
        """校验并发创建恢复命令会复用同一幂等记录。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果并发提交触发唯一键异常或创建多条命令。

        副作用:
            创建临时 SQLite 数据库并用两个线程写入恢复命令。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            logger = _null_logger("resume-concurrent")
            run_store = DurableRunStore(database_path, logger)
            run = run_store.create_for_task("task-concurrent-resume", thread_id="thread-concurrent-resume")

            with ThreadPoolExecutor(max_workers=2) as executor:
                commands = list(
                    executor.map(
                        lambda _: run_store.create_resume_command(
                            run.run_id,
                            "approve_tool",
                            {"approval_id": "approval-concurrent"},
                            "resume-concurrent-key",
                        ),
                        range(2),
                    )
                )

            self.assertEqual({item.command_id for item in commands}, {commands[0].command_id})

    def test_resume_command_claim_is_atomic_and_recoverable(self) -> None:
        """校验恢复命令只会被一个消费者领取，并可从中断状态重新排队。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果命令重复领取或无法恢复为待处理状态。

        副作用:
            创建临时 SQLite 恢复命令记录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            logger = _null_logger("resume-claim")
            run_store = DurableRunStore(database_path, logger)
            run = run_store.create_for_task("task-claim", thread_id="thread-claim")
            command = run_store.create_resume_command(
                run.run_id,
                "approve_tool",
                {"approval_id": "approval-claim"},
                "claim-key",
            )

            first_claim = run_store.claim_pending_resume_commands(actions=("approve_tool",))
            second_claim = run_store.claim_pending_resume_commands(actions=("approve_tool",))
            requeued = run_store.requeue_processing_resume_commands()
            recovered_claim = run_store.claim_pending_resume_commands(actions=("approve_tool",))

            self.assertEqual([item.command_id for item in first_claim], [command.command_id])
            self.assertEqual(second_claim, [])
            self.assertEqual(requeued, 1)
            self.assertEqual([item.command_id for item in recovered_claim], [command.command_id])

    def test_checkpointer_wrapper_closes_sqlite_connection(self) -> None:
        """校验 LangGraph checkpointer 包装对象会关闭底层连接。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 close 后连接仍可使用。

        副作用:
            创建并关闭一个临时 SQLite 连接。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            connection = sqlite3.connect(Path(temp_dir) / "checkpointer.sqlite3")
            wrapper = ManagedSqliteCheckpointer(saver=object(), connection=connection)

            wrapper.close()

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_human_input_response_creates_resume_command(self) -> None:
        """校验人工输入响应会创建恢复命令。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果响应未持久化或未产生恢复命令。

        副作用:
            创建临时 SQLite 数据库并写入 human input 记录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            logger = _null_logger("human")
            run_store = DurableRunStore(database_path, logger)
            dispatcher = ResumeDispatcher(run_store, logger)
            service = HumanInputService(
                human_input_store=HumanInputStore(database_path),
                run_store=run_store,
                resume_dispatcher=dispatcher,
                logger=logger,
            )
            run = run_store.create_for_task("task-2", thread_id="thread-2")

            request = service.request_input(run.run_id, "继续吗？", {"type": "object"})
            response = service.respond(request.request_id, {"answer": "yes"}, "human-key")

            self.assertEqual(response.request_id, request.request_id)
            self.assertEqual(run_store.get(run.run_id).status, "resuming")
            self.assertIsNotNone(run_store.get_resume_command_by_key("resume:human-key"))

    def test_tool_execution_policy_defaults_to_approval_required(self) -> None:
        """校验没有具体 provider 时工具默认需要审批。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果默认策略未要求审批。

        副作用:
            创建临时 SQLite 数据库并写入工具调用记录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            logger = _null_logger("tool")
            service = ToolExecutionService(
                store=ToolExecutionStore(database_path),
                policy=ToolExecutionPolicy(),
                logger=logger,
            )

            call, decision = service.plan_tool_call(
                run_id="run-1",
                tool_name="execute_command",
                arguments={"argv": ["pytest"]},
                permission="command",
                idempotency_key="tool-key",
            )

            self.assertEqual(decision.status, "approval_required")
            self.assertEqual(call.status, "waiting_approval")

    def test_artifact_file_and_metadata_are_stored(self) -> None:
        """校验 artifact 内容和元数据能分别落盘。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 artifact 文件或元数据不符合预期。

        副作用:
            创建临时 artifact 文件和 SQLite 记录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            relative_path, size_bytes, digest = ArtifactFileStore(root / "artifacts").write_text(
                "command_output",
                "hello",
            )
            record = ArtifactStore(root / "app.sqlite3").create(
                run_id="run-1",
                kind="command_output",
                mime_type="text/plain",
                storage_path=relative_path,
                size_bytes=size_bytes,
                sha256=digest,
                summary="hello",
            )

            self.assertEqual(record.size_bytes, 5)
            self.assertTrue((root / "artifacts" / relative_path).exists())

    def test_recovery_manager_lists_waiting_runs(self) -> None:
        """校验恢复管理器能列出等待中的运行。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果等待运行未被返回。

        副作用:
            创建临时 SQLite 数据库并写入运行状态。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            logger = _null_logger("recovery")
            run_store = DurableRunStore(database_path, logger)
            run = run_store.create_for_task("task-3", thread_id="thread-3")
            run_store.mark_status(run.run_id, "waiting", wait_reason="approval")

            runs = RecoveryManager(run_store, logger).reconcile()

            self.assertEqual([item.run_id for item in runs], [run.run_id])


def _null_logger(name: str) -> logging.Logger:
    """创建不输出日志的测试 logger。

    参数:
        name: logger 名称后缀。

    返回:
        已配置 NullHandler 的 logger。

    异常:
        无。

    副作用:
        清空指定 logger 的 handler 并添加 NullHandler。
    """

    logger = logging.getLogger(f"test-durable-{name}")
    logger.handlers = []
    logger.addHandler(logging.NullHandler())
    return logger


if __name__ == "__main__":
    unittest.main()

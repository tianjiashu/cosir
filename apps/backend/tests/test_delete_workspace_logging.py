"""删除 workspace 链路的结构化日志验证。

按《通用日志开发规范》校验删除工作区时，运行时、审批、durable run、trace 与
工作区存储各层都会输出稳定的 event 日志，且业务实体 ID 只进 data、不进顶层。
"""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.models.echo import EchoStreamingModelAdapter
from app.core.runtime.runner import AgentRuntime
from app.storage.crud.task import SQLiteTaskStore
from app.storage.crud.durable import DurableRunStore
from app.storage.crud.approval import ApprovalStore
from app.storage.crud.trace import TraceStore
from app.core.trace.recorder import TraceRecorder
from app.core.runs.resume import ResumeDispatcher
from app.domain.approvals.service import ApprovalService
from app.tools.registry.memory import ToolRegistry
from app.tools.tool_handler.safe_read import SafeReadTools
from app.tools.runtime.compatibility import ToolScheduler


class _CaptureHandler(logging.Handler):
    """同步捕获日志记录，便于断言 event 名与 data 字段。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """追加一条日志记录到内存列表。"""
        self.records.append(record)


class DeleteWorkspaceLoggingTests(unittest.TestCase):
    """校验删除 workspace 全链路会按规范输出结构化日志。"""

    def _build_runtime(self, temp_dir: Path, logger: logging.Logger) -> AgentRuntime:
        """构造一个带完整存储与日志器的运行时。

        参数:
            temp_dir: 用作项目根与数据库路径的临时目录。
            logger: 统一日志器，所有存储层共享。

        返回:
            已装配的 AgentRuntime。

        异常:
            无。

        副作用:
            创建临时目录下的 SQLite schema。
        """
        database_file = temp_dir / "app.sqlite3"
        task_store = SQLiteTaskStore(database_file)
        trace_store = TraceStore(database_file)
        trace_recorder = TraceRecorder(trace_store, logger)
        run_store = DurableRunStore(database_file)
        resume_dispatcher = ResumeDispatcher(run_store, logger, trace_recorder=trace_recorder)
        approval_store = ApprovalStore(database_file)
        approval_service = ApprovalService(
            approval_store=approval_store,
            run_store=run_store,
            resume_dispatcher=resume_dispatcher,
            logger=logger,
            trace_recorder=trace_recorder,
        )
        safe_tools = SafeReadTools(temp_dir)
        registry = ToolRegistry(safe_tools.definitions())
        tool_scheduler = ToolScheduler(
            registry=registry,
            allowed_permissions=("safe_read",),
            logger=logger,
        )
        return AgentRuntime(
            settings=BackendSettings(
                project_root=temp_dir,
                log_dir=temp_dir / "logs",
                database_file=database_file,
            ),
            task_store=task_store,
            context_builder=TextContextBuilder(),
            model_adapter=EchoStreamingModelAdapter(),
            tool_scheduler=tool_scheduler,
            logger=logger,
            run_store=run_store,
            approval_service=approval_service,
            trace_recorder=trace_recorder,
        )

    def test_delete_workspace_logs_full_chain(self) -> None:
        """删除工作区应触发全链路结构化日志且实体 ID 进 data。"""
        logger = logging.getLogger("coding_agent.backend")
        logger.handlers = []
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        capture = _CaptureHandler()
        logger.addHandler(capture)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                runtime = self._build_runtime(Path(temp_dir), logger)
                workspace = runtime.create_workspace("ws", str(temp_dir))
                task = runtime.create_task("do something", workspace_id=workspace.workspace_id)
                run = runtime._run_store.create_for_turn(task.task_id, task.latest_turn_id, "pending")
                runtime._approval_service.request_approval(
                    run.run_id, "write_file", "write", "medium", {"note": "verify"}
                )

                runtime.delete_workspace(workspace.workspace_id)

                events = [record.msg for record in capture.records]
                expected_events = [
                    "workspace_delete_start",
                    "approval_data_deleted",
                    "durable_runs_deleted",
                    "task_traces_deleted",
                    "workspace_cascade_deleted",
                    "workspace_deleted",
                ]
                for event in expected_events:
                    self.assertIn(event, events, f"删除链路缺少日志事件: {event}")

                deleted = next(r for r in capture.records if r.msg == "workspace_deleted")
                self.assertEqual(getattr(deleted, "data", {}).get("workspace_id"), workspace.workspace_id)
                self.assertIn(task.task_id, getattr(deleted, "data", {}).get("task_ids", []))

                # 校验实体 ID 仅出现在 data，不污染顶层链路键
                self.assertNotIn("workspace_id", vars(deleted))
        finally:
            logger.removeHandler(capture)

    def test_delete_workspace_logs_failure_and_reraises(self) -> None:
        """审批数据删除失败时，应记录 ERROR 现场并向上重抛。"""
        logger = logging.getLogger("coding_agent.backend")
        logger.handlers = []
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        capture = _CaptureHandler()
        logger.addHandler(capture)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                runtime = self._build_runtime(Path(temp_dir), logger)
                workspace = runtime.create_workspace("ws", str(temp_dir))
                task = runtime.create_task("do something", workspace_id=workspace.workspace_id)
                run = runtime._run_store.create_for_turn(task.task_id, task.latest_turn_id, "pending")
                runtime._approval_service.request_approval(
                    run.run_id, "write_file", "write", "medium", {"note": "verify"}
                )

                with patch(
                    "app.storage.crud.approval.ApprovalStore.delete_by_run_ids",
                    side_effect=RuntimeError("db is locked"),
                ):
                    with self.assertRaises(RuntimeError):
                        runtime.delete_workspace(workspace.workspace_id)

                events = [record.msg for record in capture.records]
                self.assertIn("workspace_delete_start", events)
                self.assertIn("approval_data_delete_failed", events)
                failed = next(r for r in capture.records if r.msg == "approval_data_delete_failed")
                self.assertEqual(failed.levelname, "ERROR")
                self.assertIsNotNone(failed.exc_info)
        finally:
            logger.removeHandler(capture)


if __name__ == "__main__":
    unittest.main()

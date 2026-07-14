"""针对任务创建与 SSE 流式传输的 FastAPI API 测试。"""

import importlib.util
import logging
from pathlib import Path
import tempfile
import unittest

from app.domain.approvals.service import ApprovalService
from app.domain.approvals.store import ApprovalStore
from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.models.echo import EchoStreamingModelAdapter
from app.core.runs.recovery import RecoveryManager
from app.core.runs.resume import ResumeDispatcher
from app.core.runs.store import DurableRunStore
from app.core.runtime.runner import AgentRuntime
from app.storage.sqlite import SQLiteTaskStore
from app.tools.registry import ToolRegistry
from app.tools.safe_read import SafeReadTools
from app.tools.scheduler import ToolScheduler


FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class BackendApiTests(unittest.TestCase):
    """在 FastAPI 依赖可用时校验 API 路由。"""

    def setUp(self) -> None:
        """初始化 API 测试使用的临时目录。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建一个用于持有临时目录句柄的列表，使其保持存活。
        """

        self._temp_dirs = []

    def tearDown(self) -> None:
        """清理 API 测试使用的临时目录。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            删除测试创建的临时目录。
        """

        for temp_dir in self._temp_dirs:
            temp_dir.cleanup()

    def test_task_creation_rejects_null_text(self) -> None:
        """校验 API 校验会拒绝空任务文本。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果接受了空文本。

        副作用:
            创建一个进程内的 FastAPI 测试客户端。
        """

        client = self._build_client()
        response = client.post("/tasks", json={"text": None})

        self.assertEqual(response.status_code, 422)

    def test_health_reports_backend_model_configuration(self) -> None:
        """校验健康检查端点会暴露当前模型配置摘要。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果健康状态缺少 provider 或 Key 配置摘要。

        副作用:
            创建一个进程内的 FastAPI 测试客户端。
        """

        client = self._build_client()
        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["model_provider"], "echo")
        self.assertFalse(payload["has_model_api_key"])

    def test_task_stream_emits_sse_and_does_not_rerun_completed_task(self) -> None:
        """校验任务流会发出 SSE，且重复读取流会重放事件。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果流式传输失败，或重复读取追加了事件。

        副作用:
            在应用的测试数据库中创建任务状态。
        """

        client = self._build_client()
        create_response = client.post("/tasks", json={"text": "hello api"})
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]
        self.assertEqual(create_response.json()["agent_id"], "developer")

        first_stream = client.get(f"/tasks/{task_id}/stream")
        second_stream = client.get(f"/tasks/{task_id}/stream")
        events_response = client.get(f"/tasks/{task_id}/events")
        checkpoints_response = client.get(f"/tasks/{task_id}/checkpoints")

        self.assertEqual(first_stream.status_code, 200)
        self.assertIn("event: run_started", first_stream.text)
        self.assertIn("event: run_finished", first_stream.text)
        self.assertEqual(second_stream.status_code, 200)
        self.assertEqual(events_response.status_code, 200)
        self.assertEqual(checkpoints_response.status_code, 200)
        self.assertEqual(len(events_response.json()), first_stream.text.count("event: "))
        self.assertGreaterEqual(len(checkpoints_response.json()), 2)

    def test_approval_decision_endpoint_returns_serializable_payload(self) -> None:
        """校验审批决策 API 返回可序列化决策并触发恢复命令消费。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果审批决策出口返回错误或恢复命令未完成消费。

        副作用:
            创建一个带 Durable Run State 的进程内 FastAPI 测试客户端。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        runtime, run_store, approval_service = self._build_runtime_with_approvals()
        task = runtime.create_task("approval api")
        run = run_store.get_by_task(task.task_id)
        self.assertIsNotNone(run)
        approval = approval_service.request_approval(
            run_id=run.run_id,
            tool_name="write_marker",
            permission="write_file",
            risk_level="high",
            payload={"arguments": {"path": "marker.txt"}},
        )
        client = TestClient(create_app(runtime=runtime))

        response = client.post(
            f"/approvals/{approval.approval_id}/decision",
            json={"decision": "approved", "reason": "ok", "idempotency_key": "api-decision-key"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["approval_id"], approval.approval_id)
        self.assertEqual(payload["decision"], "approved")
        command = run_store.get_resume_command_by_key("resume:api-decision-key")
        self.assertIsNotNone(command)
        self.assertEqual(command.status, "applied")
        self.assertEqual(runtime.get_task(task.task_id).status, "completed")
        self.assertEqual(run_store.get(run.run_id).status, "completed")

    def test_recoverable_runs_endpoint_is_strictly_read_only(self) -> None:
        """校验恢复查询接口不会消费或重排恢复命令。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果只读查询领取、重排或应用了恢复命令。

        副作用:
            创建带 processing resume command 的临时运行记录并调用 API。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        runtime, run_store, _approval_service = self._build_runtime_with_approvals()
        task = runtime.create_task("recoverable api")
        run = run_store.get_by_task(task.task_id)
        self.assertIsNotNone(run)
        run_store.mark_status(run.run_id, "waiting", wait_reason="approval")
        created = run_store.create_resume_command(
            run.run_id,
            "approve_tool",
            {"approval_id": "approval-pending"},
            "recoverable-query-key",
        )
        claimed = run_store.claim_pending_resume_commands(run.run_id, actions=("approve_tool",))
        self.assertEqual([command.command_id for command in claimed], [created.command_id])
        client = TestClient(create_app(runtime=runtime))

        response = client.get("/runs/recoverable")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(run_store.get_resume_command_by_key(created.idempotency_key).status, "processing")

    def test_resume_run_endpoint_consumes_processing_approved_command(self) -> None:
        """校验显式恢复接口会恢复 processing 状态的 approved 命令。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果恢复命令未重排应用或任务运行状态仍悬挂。

        副作用:
            创建审批决策和恢复命令，并通过 API 显式恢复运行。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        runtime, run_store, approval_service = self._build_runtime_with_approvals()
        task = runtime.create_task("resume api")
        run = run_store.get_by_task(task.task_id)
        self.assertIsNotNone(run)
        approval = approval_service.request_approval(
            run_id=run.run_id,
            tool_name="write_marker",
            permission="write_file",
            risk_level="high",
            payload={"arguments": {"path": "marker.txt"}},
        )
        approval_service.decide(approval.approval_id, "approved", "ok", "crash-window-key")
        claimed = run_store.claim_pending_resume_commands(run.run_id, actions=("approve_tool",))
        self.assertEqual(len(claimed), 1)
        self.assertEqual(run_store.get(run.run_id).status, "resuming")
        self.assertEqual(run_store.get_resume_command_by_key("resume:crash-window-key").status, "processing")
        client = TestClient(create_app(runtime=runtime))

        response = client.post(f"/runs/{run.run_id}/resume")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(run_store.get_resume_command_by_key("resume:crash-window-key").status, "applied")
        self.assertEqual(runtime.get_task(task.task_id).status, "completed")
        self.assertEqual(run_store.get(run.run_id).status, "completed")

    def test_resume_run_endpoint_consumes_processing_denied_command(self) -> None:
        """校验显式恢复接口会恢复 processing 状态的 denied 命令。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 denied 恢复命令未应用或任务运行未失败收束。

        副作用:
            创建 denied 审批决策，将恢复命令模拟为 processing，并通过 API 显式恢复运行。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        runtime, run_store, approval_service = self._build_runtime_with_approvals()
        task = runtime.create_task("denied resume api")
        run = run_store.get_by_task(task.task_id)
        self.assertIsNotNone(run)
        approval = approval_service.request_approval(
            run_id=run.run_id,
            tool_name="write_marker",
            permission="write_file",
            risk_level="high",
            payload={"arguments": {"path": "marker.txt"}},
        )
        approval_service.decide(approval.approval_id, "denied", "no", "crash-denied-key")
        claimed = run_store.claim_pending_resume_commands(run.run_id, actions=("deny_tool",))
        self.assertEqual(len(claimed), 1)
        self.assertEqual(run_store.get(run.run_id).status, "resuming")
        self.assertEqual(run_store.get_resume_command_by_key("resume:crash-denied-key").status, "processing")
        client = TestClient(create_app(runtime=runtime))

        response = client.post(f"/runs/{run.run_id}/resume")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(run_store.get_resume_command_by_key("resume:crash-denied-key").status, "applied")
        self.assertEqual(runtime.get_task(task.task_id).status, "failed")
        self.assertEqual(run_store.get(run.run_id).status, "failed")

    def test_approval_denial_endpoint_marks_task_and_run_failed(self) -> None:
        """校验拒绝审批会收束任务和运行到失败态。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果拒绝审批后状态仍悬挂或恢复命令未应用。

        副作用:
            创建审批请求，并通过 API 写入 denied 决策。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        runtime, run_store, approval_service = self._build_runtime_with_approvals()
        task = runtime.create_task("denied approval api")
        run = run_store.get_by_task(task.task_id)
        self.assertIsNotNone(run)
        approval = approval_service.request_approval(
            run_id=run.run_id,
            tool_name="write_marker",
            permission="write_file",
            risk_level="high",
            payload={"arguments": {"path": "marker.txt"}},
        )
        client = TestClient(create_app(runtime=runtime))

        response = client.post(
            f"/approvals/{approval.approval_id}/decision",
            json={"decision": "denied", "reason": "no", "idempotency_key": "api-denied-key"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(run_store.get_resume_command_by_key("resume:api-denied-key").status, "applied")
        self.assertEqual(runtime.get_task(task.task_id).status, "failed")
        self.assertEqual(run_store.get(run.run_id).status, "failed")

    def test_resume_command_not_applied_when_finalize_fails(self) -> None:
        """校验状态收束失败时恢复命令不会提前标记为 applied。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 finalize 失败后命令被标记为 applied 或状态被错误推进。

        副作用:
            创建审批恢复命令，并临时注入一次 run 状态更新失败。
        """

        runtime, run_store, approval_service = self._build_runtime_with_approvals()
        task = runtime.create_task("finalize failure api")
        run = run_store.get_by_task(task.task_id)
        self.assertIsNotNone(run)
        approval = approval_service.request_approval(
            run_id=run.run_id,
            tool_name="write_marker",
            permission="write_file",
            risk_level="high",
            payload={"arguments": {"path": "marker.txt"}},
        )
        approval_service.decide(approval.approval_id, "approved", "ok", "finalize-fail-key")
        claimed = run_store.claim_pending_resume_commands(run.run_id, actions=("approve_tool",))
        self.assertEqual(len(claimed), 1)
        original_mark_status = run_store.mark_status

        def fail_terminal_once(run_id, status, **kwargs):
            """在终态收束时模拟一次数据库写入失败。

            参数:
                run_id: 被更新的运行标识符。
                status: 目标运行状态。
                kwargs: 透传给原始 mark_status 的可选状态参数。

            返回:
                原始 mark_status 的返回值。

            异常:
                RuntimeError: 当首次进入 terminal 状态时抛出。

            副作用:
                首次 terminal 更新失败；其他状态更新走原始实现。
            """

            if status in {"completed", "failed", "cancelled"}:
                raise RuntimeError("injected finalize failure")
            return original_mark_status(run_id, status, **kwargs)

        run_store.mark_status = fail_terminal_once
        try:
            runtime.resume_run(run.run_id)
        finally:
            run_store.mark_status = original_mark_status

        self.assertEqual(run_store.get_resume_command_by_key("resume:finalize-fail-key").status, "pending")
        self.assertEqual(runtime.get_task(task.task_id).status, "pending")
        self.assertEqual(run_store.get(run.run_id).status, "resuming")

        runtime.resume_run(run.run_id)

        self.assertEqual(run_store.get_resume_command_by_key("resume:finalize-fail-key").status, "applied")
        self.assertEqual(runtime.get_task(task.task_id).status, "completed")
        self.assertEqual(run_store.get(run.run_id).status, "completed")

    def _build_client(self):
        """为后端应用构建 FastAPI TestClient。

        参数:
            无。

        返回:
            后端 API 的 TestClient 实例。

        异常:
            RuntimeError: 如果 FastAPI 依赖不可用。

        副作用:
            初始化应用运行时依赖项。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        return TestClient(create_app(runtime=self._build_runtime()))

    def _build_runtime(self) -> AgentRuntime:
        """为 API 测试构建一个隔离的运行时。

        参数:
            无。

        返回:
            带有临时项目根目录、数据库与日志路径的 AgentRuntime。

        异常:
            无。

        副作用:
            创建一个临时目录与内存中的日志记录器引用。
        """

        temp_dir = tempfile.TemporaryDirectory()
        self._temp_dirs.append(temp_dir)
        project_root = Path(temp_dir.name)
        logger = logging.getLogger(f"test-api-{id(temp_dir)}")
        logger.handlers = []
        logger.addHandler(logging.NullHandler())
        safe_tools = SafeReadTools(project_root)
        registry = ToolRegistry(safe_tools.definitions())
        return AgentRuntime(
            settings=BackendSettings(
                project_root=project_root,
                log_file=project_root / "app.log",
                database_file=project_root / "app.sqlite3",
            ),
            task_store=SQLiteTaskStore(project_root / "app.sqlite3"),
            context_builder=TextContextBuilder(),
            model_adapter=EchoStreamingModelAdapter(),
            tool_scheduler=ToolScheduler(
                registry=registry,
                allowed_permissions=("safe_read",),
                logger=logger,
            ),
            logger=logger,
        )

    def _build_runtime_with_approvals(self) -> tuple[AgentRuntime, DurableRunStore, ApprovalService]:
        """为 API 审批测试构建带 Durable Run State 的运行时。

        参数:
            无。

        返回:
            AgentRuntime、DurableRunStore 和 ApprovalService。

        异常:
            无。

        副作用:
            创建临时目录、SQLite 数据库和进程内运行时依赖。
        """

        temp_dir = tempfile.TemporaryDirectory()
        self._temp_dirs.append(temp_dir)
        project_root = Path(temp_dir.name)
        database = project_root / "app.sqlite3"
        logger = logging.getLogger(f"test-api-approval-{id(temp_dir)}")
        logger.handlers = []
        logger.addHandler(logging.NullHandler())
        safe_tools = SafeReadTools(project_root)
        registry = ToolRegistry(safe_tools.definitions())
        run_store = DurableRunStore(database, logger)
        approval_service = ApprovalService(
            approval_store=ApprovalStore(database),
            run_store=run_store,
            resume_dispatcher=ResumeDispatcher(run_store, logger),
            logger=logger,
        )
        runtime = AgentRuntime(
            settings=BackendSettings(
                project_root=project_root,
                log_file=project_root / "app.log",
                database_file=database,
            ),
            task_store=SQLiteTaskStore(database),
            context_builder=TextContextBuilder(),
            model_adapter=EchoStreamingModelAdapter(),
            tool_scheduler=ToolScheduler(
                registry=registry,
                allowed_permissions=("safe_read",),
                logger=logger,
            ),
            logger=logger,
            run_store=run_store,
            approval_service=approval_service,
            recovery_manager=RecoveryManager(run_store, logger),
        )
        return runtime, run_store, approval_service


if __name__ == "__main__":
    unittest.main()

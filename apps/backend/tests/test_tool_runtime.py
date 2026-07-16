"""Tool v2 Runtime、持久化、审批与并发边界测试。"""

from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import tempfile
import time
import unittest

from app.domain.approvals.service import ApprovalService
from app.domain.approvals.store import ApprovalStore
from app.domain.artifacts.files import ArtifactFileStore
from app.domain.artifacts.service import ArtifactService
from app.domain.artifacts.store import ArtifactStore
from app.core.runs.resume import ResumeDispatcher
from app.core.runs.store import DurableRunStore
from app.tools.execution.policy import ToolExecutionPolicy
from app.tools.execution.policy_provider import PermissionPolicyProvider
from app.tools.execution.service import ToolExecutionService
from app.tools.execution.store import ToolExecutionStore
from app.tools.runtime.concurrency import ToolConcurrentScheduler
from app.tools.executor import ToolCallExecutor
from app.tools.runtime.locks import ToolResourceLockManager
from app.tools.registry.memory import ToolRegistry
from app.tools.results import ToolObservationBuilder
from app.tools.runtime.platform import ToolExecutionContext, ToolRuntime
from app.tools.catalog.selector import ToolSelectionContext, ToolSelector
from app.tools.catalog.records import ToolCatalogRecord
from app.tools.types import ArtifactRequest, ToolCall, ToolDefinition


class ToolRuntimeTests(unittest.TestCase):
    """验证 Tool v2 平台不会绕开策略、记录与资源锁。"""

    def test_approval_blocks_side_effect_and_persists_wait(self) -> None:
        """校验危险工具在审批前不会写入文件。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果工具副作用先于审批发生。

        副作用:
            创建临时 Durable Run 数据库与审批记录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "marker.txt"
            runtime, run_store, _ = self._build_persistent_runtime(root, self._write_tool())
            run = run_store.create_for_task("task-approval", thread_id="thread-approval")

            observation = runtime.execute_single_tool_call(
                ToolCall("write_marker", {"path": str(marker)}),
                ToolExecutionContext(run.run_id, "step-approval"),
            )

            self.assertEqual(observation.status, "approval_required")
            self.assertFalse(marker.exists())
            self.assertEqual(run_store.get(run.run_id).status, "waiting")

    def test_artifact_request_is_only_persisted_by_executor(self) -> None:
        """校验 handler 返回的 ArtifactRequest 由执行器统一写入。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果未创建 artifact 或观测未关联 artifact。

        副作用:
            写入临时 artifact 文件和 SQLite 元数据。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool = ToolDefinition(
                name="large_output",
                description="Return a large captured output artifact.",
                permission="safe_read",
                required_params=(),
                handler=_artifact_handler,
                parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
            )
            runtime, run_store, _ = self._build_persistent_runtime(root, tool)
            run = run_store.create_for_task("task-artifact", thread_id="thread-artifact")

            observation = runtime.execute_single_tool_call(
                ToolCall("large_output", {}),
                ToolExecutionContext(run.run_id, "step-artifact"),
            )

            self.assertEqual(observation.status, "success")
            self.assertIsNotNone(observation.artifact_id)
            self.assertIn("artifact_id=", observation.content)
            self.assertTrue(any((root / "artifacts").rglob("*.txt")))

    def test_idempotency_does_not_repeat_completed_side_effect(self) -> None:
        """校验同一 run、step 与参数的重试不会重复执行 handler。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 handler 被调用超过一次。

        副作用:
            写入临时 SQLite 调用记录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            counter_path = root / "counter.txt"
            tool = ToolDefinition(
                name="count_once",
                description="Increment a process-local test counter.",
                permission="safe_read",
                required_params=("path",),
                handler=_increment_counter,
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )
            runtime, run_store, _ = self._build_persistent_runtime(root, tool)
            run = run_store.create_for_task("task-idempotent", thread_id="thread-idempotent")
            context = ToolExecutionContext(run.run_id, "step-idempotent")

            call = ToolCall("count_once", {"path": str(counter_path)})
            runtime.execute_single_tool_call(call, context)
            retry = runtime.execute_single_tool_call(call, context)

            self.assertEqual(counter_path.read_text(encoding="utf-8"), "1")
            self.assertEqual(retry.status, "success")
            self.assertIn("not repeated", retry.content)

    def test_approved_request_resumes_original_call_once(self) -> None:
        """校验已批准的持久化审批只恢复原始工具调用一次。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果恢复未执行或重复执行副作用。

        副作用:
            创建审批决策并写入临时标记文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "approved.txt"
            runtime, run_store, approval_service = self._build_persistent_runtime(root, self._write_tool())
            run = run_store.create_for_task("task-resume", thread_id="thread-resume")
            context = ToolExecutionContext(run.run_id, "step-resume")
            call = ToolCall("write_marker", {"path": str(marker)})
            waiting = runtime.execute_single_tool_call(call, context)
            pending = approval_service.list_pending(run.run_id)[0]
            approval_service.decide(pending.approval_id, "approved", "approved", "resume-key")

            resumed = runtime.resume_approved_tool_call(call, context, pending.approval_id)

            self.assertEqual(waiting.status, "approval_required")
            self.assertEqual(resumed.status, "success")
            self.assertEqual(marker.read_text(encoding="utf-8"), "written")

    def test_repeated_approved_resume_is_idempotent(self) -> None:
        """校验同一已批准审批被重复消费时不会重复工具副作用。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果重复恢复抛出异常或重复写入副作用。

        副作用:
            创建临时审批记录和标记文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "repeat-approved.txt"
            runtime, run_store, approval_service = self._build_persistent_runtime(root, self._write_tool())
            run = run_store.create_for_task("task-repeat-resume", thread_id="thread-repeat-resume")
            context = ToolExecutionContext(run.run_id, "step-repeat-resume")
            call = ToolCall("write_marker", {"path": str(marker)})
            runtime.execute_single_tool_call(call, context)
            approval = approval_service.list_pending(run.run_id)[0]
            approval_service.decide(approval.approval_id, "approved", "approved", "repeat-resume-key")

            first = runtime.resume_approved_tool_call(call, context, approval.approval_id)
            second = runtime.resume_approved_tool_call(call, context, approval.approval_id)

            self.assertEqual(first.status, "success")
            self.assertEqual(second.status, "success")
            self.assertIn("not repeated", second.content)
            self.assertEqual(marker.read_text(encoding="utf-8"), "written")

    def test_concurrent_tool_call_creation_reuses_single_record(self) -> None:
        """校验并发创建同一工具调用不会触发唯一键异常。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果并发写入创建多条记录或抛出 IntegrityError。

        副作用:
            创建临时 SQLite 数据库并用两个线程写入工具调用记录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            store = ToolExecutionStore(Path(temp_dir) / "app.sqlite3")

            with ThreadPoolExecutor(max_workers=2) as executor:
                calls = list(
                    executor.map(
                        lambda _: store.create_tool_call(
                            run_id="run-concurrent-tool",
                            step_id="step-concurrent-tool",
                            tool_name="safe_read",
                            arguments={"path": "README.md"},
                            permission="safe_read",
                            idempotency_key="tool-concurrent-key",
                        ),
                        range(2),
                    )
                )

            self.assertEqual({item.tool_call_id for item in calls}, {calls[0].tool_call_id})

    def test_pending_approval_retry_reuses_single_request(self) -> None:
        """校验待审批调用重试不会重复创建审批请求。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果同一幂等调用产生多个待审批请求。

        副作用:
            创建临时工具调用与审批记录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime, run_store, approval_service = self._build_persistent_runtime(root, self._write_tool())
            run = run_store.create_for_task("task-pending-retry", thread_id="thread-pending-retry")
            context = ToolExecutionContext(run.run_id, "step-pending-retry")
            call = ToolCall("write_marker", {"path": str(root / "marker.txt")})

            first = runtime.execute_single_tool_call(call, context)
            second = runtime.execute_single_tool_call(call, context)

            self.assertEqual(first.status, "approval_required")
            self.assertEqual(second.status, "approval_required")
            self.assertEqual(len(approval_service.list_pending(run.run_id)), 1)

    def test_disjoint_tools_run_concurrently_and_results_align(self) -> None:
        """校验互不冲突的工具可并行并按 tool_call_id 返回。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果并发执行退化为串行或结果错位。

        副作用:
            在测试进程中短暂等待两个 handler。
        """

        first = self._sleep_tool("read_one", "first")
        second = self._sleep_tool("read_two", "second")
        runtime = self._build_ephemeral_runtime(first, second)
        started = time.monotonic()
        observations = runtime.execute_tool_calls(
            (
                ToolCall("read_one", {"content": "first"}, "first-id"),
                ToolCall("read_two", {"content": "second"}, "second-id"),
            )
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.35)
        self.assertEqual([item.content for item in observations], ["first", "second"])
        self.assertEqual([item.tool_call_id for item in observations], ["first-id", "second-id"])

    def test_selector_honors_permission_and_limit(self) -> None:
        """校验 Tool Selector 不会暴露未授权或超额工具。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果选择结果包含未授权工具。

        副作用:
            无。
        """

        read = self._sleep_tool("read_one", "first")
        write = ToolDefinition("write_one", "Write", "write_file", (), _empty_handler, {})
        selected = ToolSelector().select(
            (
                ToolCatalogRecord("write_one", "file_edit", "write", permission="write_file"),
                ToolCatalogRecord("read_one", "file_read", "read", permission="safe_read"),
            ),
            (read, write),
            ToolSelectionContext("read", ("safe_read",), max_tools=1),
        )

        self.assertEqual([item.name for item in selected], ["read_one"])

    def _build_persistent_runtime(
        self, root: Path, *tools: ToolDefinition
    ) -> tuple[ToolRuntime, DurableRunStore, ApprovalService]:
        """构造接入 Durable Run、审批、artifact 的测试 Runtime。

        参数:
            root: 临时测试根目录。
            tools: 需要注册的工具定义。

        返回:
            Tool Runtime、关联 Durable Run 仓储和审批服务。

        异常:
            OSError: 如果临时文件无法创建。

        副作用:
            初始化 SQLite、artifact 目录和审批服务。
        """

        logger = _null_logger("persistent")
        database = root / "app.sqlite3"
        run_store = DurableRunStore(database, logger)
        approval_service = ApprovalService(
            ApprovalStore(database), run_store, ResumeDispatcher(run_store, logger), logger
        )
        execution_service = ToolExecutionService(
            ToolExecutionStore(database),
            ToolExecutionPolicy(
                (PermissionPolicyProvider(("safe_read",), ("write_file",)),)
            ),
            logger,
        )
        builder = ToolObservationBuilder()
        return (
            ToolRuntime(
                ToolRegistry(tools),
                ToolCallExecutor(
                    builder,
                    logger,
                    ArtifactService(ArtifactStore(database), ArtifactFileStore(root / "artifacts")),
                    execution_service,
                ),
                builder,
                logger,
                execution_service=execution_service,
                approval_service=approval_service,
                concurrent_scheduler=ToolConcurrentScheduler(ToolResourceLockManager(), logger),
            ),
            run_store,
            approval_service,
        )

    def _build_ephemeral_runtime(self, *tools: ToolDefinition) -> ToolRuntime:
        """构造不写 SQLite 的并发测试 Runtime。

        参数:
            tools: 需要注册的工具定义。

        返回:
            使用进程内锁的 Tool Runtime。

        异常:
            无。

        副作用:
            创建线程池调度器。
        """

        logger = _null_logger("ephemeral")
        builder = ToolObservationBuilder()
        return ToolRuntime(
            ToolRegistry(tools),
            ToolCallExecutor(builder, logger),
            builder,
            logger,
            concurrent_scheduler=ToolConcurrentScheduler(ToolResourceLockManager(), logger),
        )

    def _write_tool(self) -> ToolDefinition:
        """构造会写入标记文件的危险测试工具。

        参数:
            无。

        返回:
            带 write_file 权限的工具定义。

        异常:
            无。

        副作用:
            无；实际副作用由 handler 执行时发生。
        """

        return ToolDefinition(
            "write_marker",
            "Write marker",
            "write_file",
            ("path",),
            _write_marker,
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def _sleep_tool(self, name: str, content: str) -> ToolDefinition:
        """构造短暂休眠后返回固定文本的只读测试工具。

        参数:
            name: 工具名。
            content: handler 返回文本。

        返回:
            可并发安全读取工具定义。

        异常:
            无。

        副作用:
            handler 运行时会短暂休眠。
        """

        return ToolDefinition(
            name,
            name,
            "safe_read",
            ("content",),
            _sleep_and_return,
            {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
                "additionalProperties": False,
            },
        )


def _artifact_handler() -> ArtifactRequest:
    """返回供 ToolCallExecutor 统一持久化的测试 artifact 请求。

    参数:
        无。

    返回:
        一条命令输出 artifact 请求。

    异常:
        无。

    副作用:
        无。
    """

    return ArtifactRequest("command_output", "full command output", "captured command output")


def _write_marker(path: str) -> str:
    """写入测试标记文件。

    参数:
        path: 需要创建的临时文件绝对路径。

    返回:
        固定成功文本。

    异常:
        OSError: 如果文件无法写入。

    副作用:
        写入 marker 文件。
    """

    Path(path).write_text("written", encoding="utf-8")
    return "written"


def _increment_counter(path: str) -> str:
    """递增测试进程内计数器。

    参数:
        path: 用于保存计数的临时文件绝对路径。

    返回:
        当前计数值文本。

    异常:
        OSError: 如果计数文件无法读写。

    副作用:
        更新计数文件。
    """

    counter = Path(path)
    value = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    counter.write_text(str(value + 1), encoding="utf-8")
    return str(value + 1)


def _sleep_and_return(content: str) -> str:
    """等待固定时长后返回测试文本。

    参数:
        content: 需要返回的文本。

    返回:
        原始 content。

    异常:
        无。

    副作用:
        阻塞当前 handler 约 0.2 秒。
    """

    time.sleep(0.2)
    return content


def _empty_handler() -> str:
    """返回空文本的无副作用测试 handler。

    参数:
        无。

    返回:
        空字符串。

    异常:
        无。

    副作用:
        无。
    """

    return ""


def _null_logger(name: str) -> logging.Logger:
    """创建不会写入测试输出的日志器。

    参数:
        name: 日志器名称后缀。

    返回:
        只绑定 NullHandler 的日志器。

    异常:
        无。

    副作用:
        清空同名 logger 既有 handler。
    """

    logger = logging.getLogger(f"test-tool-runtime-{name}")
    logger.handlers = []
    logger.addHandler(logging.NullHandler())
    return logger

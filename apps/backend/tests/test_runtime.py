"""针对第一版后端运行时切片的测试。"""

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, List

from app.agents.profile import AgentProfile
from app.config.settings import BackendSettings
from app.events.types import EventType
from app.context.builder import TextContextBuilder
from app.models.base import ModelDelta, ModelToolDefinition, RuntimeMessage
from app.models.echo import EchoStreamingModelAdapter
from app.runtime.operations import RuntimeOperations
from app.runtime.runner import AgentRuntime
from app.storage.sqlite import SQLiteTaskStore
from app.storage.records import TaskRecord
from app.tools.registry import ToolRegistry
from app.tools.safe_read import SafeReadTools
from app.tools.scheduler import ToolScheduler
from app.tools.types import ToolCall, ToolDefinition
from app.workflows.react_like import ReactLikeWorkflow
from app.workflows.step_controller import StepDecision


def _write_test_handler(path: str) -> str:
    """为不应到达执行的测试写入一个标记文件。

    参数:
        path: 待写入的文件系统路径。

    返回:
        标记文本。

    异常:
        OSError: 如果文件无法被写入。

    副作用:
        在执行时写入一个标记文件。
    """

    Path(path).write_text("written", encoding="utf-8")
    return "written"


class AgentRuntimeTests(unittest.TestCase):
    """校验纯文本的运行时事件循环。"""

    def test_runtime_streams_events_and_completes_task(self) -> None:
        """校验文本任务会产生模型增量并到达完成状态。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果运行时事件或最终任务状态不正确。

        副作用:
            创建一个临时的日志路径并运行一个异步运行时任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._build_runtime(Path(temp_dir))
            task = runtime.create_task("hello agent")

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(runtime.get_task(task.task_id).status, "completed")
            self.assertEqual(events[0].event_type, "run_started")
            self.assertIn("model_output_delta", [event.event_type for event in events])
            self.assertEqual(events[-1].event_type, "run_finished")

    def test_runtime_creates_state_checkpoints(self) -> None:
        """校验运行时检查点会被持久化并作为事件发出。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果检查点记录或事件缺失。

        副作用:
            创建一个临时运行时并执行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._build_runtime(Path(temp_dir))
            task = runtime.create_task("checkpoint me")

            events = asyncio.run(self._collect_events(runtime, task.task_id))
            checkpoints = runtime.list_checkpoints(task.task_id)
            checkpoint_events = [
                event for event in events if event.event_type == "checkpoint_created"
            ]

            self.assertGreaterEqual(len(checkpoints), 2)
            self.assertEqual(len(checkpoints), len(checkpoint_events))
            self.assertEqual(checkpoints[0].stage, "run_started")
            self.assertEqual(checkpoints[-1].stage, "run_finished")
            self.assertEqual(checkpoints[-1].snapshot["task"]["status"], "completed")
            self.assertEqual(checkpoints[-1].snapshot["file_change_metadata"], [])

    def test_runtime_uses_agent_profile_for_task_events_and_context(self) -> None:
        """校验 Agent 档案会被持久化、发出、检查点化并进入提示词。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果运行时执行丢失了所配置的 Agent 档案。

        副作用:
            使用一个自定义的 Agent 档案与记录型模型运行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            model = RecordingContextModel()
            agent_profile = AgentProfile(
                agent_id="reviewer",
                role="reviewer",
                goal="审查实现风险并给出明确结论。",
                allowed_tools=("safe_read",),
                context_policy="text_only_review",
            )
            runtime = self._build_runtime(
                Path(temp_dir),
                model_adapter=model,
                agent_profile=agent_profile,
            )
            task = runtime.create_task("review this")

            events = asyncio.run(self._collect_events(runtime, task.task_id))
            checkpoints = runtime.list_checkpoints(task.task_id)
            system_message = model.messages[0]

            self.assertEqual(task.agent_id, "reviewer")
            self.assertEqual(runtime.get_task(task.task_id).agent_id, "reviewer")
            self.assertEqual(events[0].payload["agent"]["agent_id"], "reviewer")
            self.assertEqual(checkpoints[-1].snapshot["agent"]["role"], "reviewer")
            self.assertEqual(checkpoints[-1].snapshot["task"]["agent_id"], "reviewer")
            self.assertEqual(system_message.role, "system")
            self.assertIn("reviewer", system_message.content_text)
            self.assertIn("safe_read", system_message.content_text)
            self.assertIn("text_only_review", system_message.content_text)

    def test_checkpoint_failure_is_logged_and_emitted(self) -> None:
        """校验检查点写入失败会被记录并流式发出。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果检查点失败缺少诊断信息或流事件。

        副作用:
            使用一个拒绝检查点写入的存储运行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            store = FailingCheckpointStore(project_root / "app.sqlite3")
            runtime = self._build_runtime(project_root, task_store=store)
            task = runtime.create_task("checkpoint failure")

            with self.assertLogs(level="ERROR") as logs:
                events = asyncio.run(self._collect_events(runtime, task.task_id))

            checkpoint_events = [
                event for event in events if event.event_type == "checkpoint_failed"
            ]
            self.assertGreaterEqual(len(checkpoint_events), 1)
            self.assertTrue(checkpoint_events[0].payload["event_persisted"])
            self.assertIn("checkpoint_failed", "\n".join(logs.output))

    def test_checkpoint_failure_event_falls_back_when_event_persistence_fails(self) -> None:
        """校验当事件持久化失败时检查点失败仍会到达活动流。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果没有发出未持久化的检查点失败事件。

        副作用:
            使用一个拒绝检查点与失败事件写入的存储运行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            store = FailingCheckpointAndFailureEventStore(project_root / "app.sqlite3")
            runtime = self._build_runtime(project_root, task_store=store)
            task = runtime.create_task("checkpoint failure fallback")

            with self.assertLogs(level="ERROR"):
                events = asyncio.run(self._collect_events(runtime, task.task_id))

            checkpoint_events = [
                event for event in events if event.event_type == "checkpoint_failed"
            ]
            self.assertGreaterEqual(len(checkpoint_events), 1)
            self.assertFalse(checkpoint_events[0].payload["event_persisted"])
            self.assertIn("event_error", checkpoint_events[0].payload)

    def test_blank_task_is_rejected(self) -> None:
        """校验空白文本输入会在运行时执行前被拒绝。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果空白输入未抛出 ValueError。

        副作用:
            创建一个临时运行时用于断言。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._build_runtime(Path(temp_dir))
            with self.assertRaises(ValueError):
                runtime.create_task("   ")

    def test_cancelled_task_does_not_run_model_step(self) -> None:
        """校验被取消的任务会发出取消事件而不产生模型增量。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果运行时为被取消的任务发出了模型输出。

        副作用:
            创建一个临时运行时并将一个任务修改为已取消状态。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._build_runtime(Path(temp_dir))
            task = runtime.create_task("hello agent")
            runtime.cancel_task(task.task_id)

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(events[0].event_type, "run_cancelled")
            self.assertNotIn("model_output_delta", [event.event_type for event in events])

    def test_completed_task_stream_replays_events_without_rerun(self) -> None:
        """校验已完成的任务在重复读取流时不会被再次执行。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果重复读取流追加了重复事件。

        副作用:
            执行一次任务，然后再次读取其流。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._build_runtime(Path(temp_dir))
            task = runtime.create_task("hello agent")

            first_events = asyncio.run(self._collect_events(runtime, task.task_id))
            second_events = asyncio.run(self._collect_events(runtime, task.task_id))
            stored_events = runtime.list_events(task.task_id)

            self.assertEqual(len(first_events), len(second_events))
            self.assertEqual(len(stored_events), len(first_events))
            self.assertEqual(runtime.get_task(task.task_id).status, "completed")

    def test_tool_call_goes_through_scheduler_and_adds_observation(self) -> None:
        """校验模型请求的工具会经过工具调度器并添加观测结果。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果缺少期望的工具事件。

        副作用:
            创建一个临时的可读取项目文件并运行一个脚本化模型。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "note.txt").write_text("tool content", encoding="utf-8")
            runtime = self._build_runtime(project_root, model_adapter=ScriptedToolModel())
            task = runtime.create_task("read note")

            events = asyncio.run(self._collect_events(runtime, task.task_id))
            event_types = [event.event_type for event in events]

            self.assertIn("tool_call_requested", event_types)
            self.assertIn("tool_call_started", event_types)
            self.assertIn("tool_call_finished", event_types)
            self.assertIn("observation_added", event_types)
            self.assertEqual(events[-1].event_type, "run_finished")
            self.assertTrue(
                any(
                    checkpoint.snapshot["tool_call_history"]
                    for checkpoint in runtime.list_checkpoints(task.task_id)
                )
            )

    def test_approval_required_tool_emits_event_and_fails_task(self) -> None:
        """校验需要审批的工具在未经 UI 审批时不会执行。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果需审批的工具在静默中继续执行。

        副作用:
            通过一个带有需审批写工具的调度器运行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            logger = logging.getLogger("test-approval-runtime")
            logger.handlers = []
            logger.addHandler(logging.NullHandler())
            scheduler = self._build_tool_scheduler(
                project_root,
                logger,
                extra_tools=(
                    ToolDefinition(
                        name="write_file",
                        description="Write test file.",
                        permission="write_file",
                        required_params=("path",),
                        handler=_write_test_handler,
                        parameters_schema={
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                    ),
                ),
                approval_required_permissions=("write_file",),
            )
            agent_profile = AgentProfile(
                agent_id="developer",
                role="developer",
                goal="Use write tools only after approval.",
                allowed_tools=("safe_read", "write_file"),
                context_policy="text_only_v1",
            )
            runtime = self._build_runtime(
                project_root,
                model_adapter=ApprovalRequiredToolModel(str(project_root / "note.txt")),
                tool_scheduler=scheduler,
                agent_profile=agent_profile,
            )
            task = runtime.create_task("write note")

            events = asyncio.run(self._collect_events(runtime, task.task_id))
            event_types = [event.event_type for event in events]

            self.assertEqual(runtime.get_task(task.task_id).status, "waiting")
            self.assertIn("tool_approval_required", event_types)
            self.assertEqual(events[-1].event_type.value, "checkpoint_created")
            self.assertFalse((project_root / "note.txt").exists())

    def test_tool_error_limit_fails_run_before_max_steps(self) -> None:
        """校验重复的工具错误会以特定的终态原因失败。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果工具错误仅被报告为达到最大步骤。

        副作用:
            运行一个反复请求未知工具的脚本化模型。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._build_runtime(
                Path(temp_dir),
                model_adapter=ScriptedFailingToolModel(),
                tool_error_limit=2,
            )
            task = runtime.create_task("use missing tool")

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(runtime.get_task(task.task_id).status, "failed")
            self.assertEqual(events[-1].event_type, "run_failed")
            self.assertEqual(events[-1].payload["error"], "tool_error_limit_reached")
            self.assert_no_running_steps(runtime, task.task_id)

    def test_max_steps_closes_running_steps(self) -> None:
        """校验达到最大步骤的终止不会留下运行中的步骤。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果达到最大步骤的失败留下了运行中的步骤。

        副作用:
            运行一个脚本化模型直到达到最大步骤。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._build_runtime(
                Path(temp_dir),
                model_adapter=ScriptedFailingToolModel(),
                max_steps=1,
                tool_error_limit=3,
            )
            task = runtime.create_task("hit max steps")

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(runtime.get_task(task.task_id).status, "failed")
            self.assertEqual(events[-1].payload["error"], "max_steps_reached")
            self.assert_no_running_steps(runtime, task.task_id)

    def test_invalid_model_output_closes_running_steps(self) -> None:
        """校验非法的模型输出失败会关闭活跃模型步骤。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果非法输出留下了运行中的步骤。

        副作用:
            运行一个不发出最终输出或工具调用的脚本化模型。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._build_runtime(Path(temp_dir), model_adapter=EmptyModel())
            task = runtime.create_task("invalid output")

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(runtime.get_task(task.task_id).status, "failed")
            self.assertEqual(events[-1].payload["error"], "invalid_model_output")
            self.assert_no_running_steps(runtime, task.task_id)

    def test_model_exception_closes_running_steps(self) -> None:
        """校验模型适配器异常会关闭活跃模型步骤。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果模型异常留下了运行中的步骤。

        副作用:
            运行一个抛出 RuntimeError 的模型适配器。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._build_runtime(Path(temp_dir), model_adapter=FailingModel())
            task = runtime.create_task("model failure")

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(runtime.get_task(task.task_id).status, "failed")
            self.assertEqual(events[-1].payload["error"], "model exploded")
            self.assert_no_running_steps(runtime, task.task_id)

    def test_context_budget_failure_closes_running_steps_before_model_io(self) -> None:
        """校验过大的上下文会在模型适配器流式传输之前失败。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果模型适配器被调用，或仍有步骤在运行。

        副作用:
            使用一个故意很小的上下文预算运行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            model = CountingModel()
            runtime = self._build_runtime(
                Path(temp_dir),
                model_adapter=model,
                max_context_chars=5,
            )
            task = runtime.create_task("context too long")

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(model.calls, 0)
            self.assertEqual(runtime.get_task(task.task_id).status, "failed")
            self.assertEqual(events[-1].event_type, "run_failed")
            self.assertIn("context_window_exceeded", events[-1].payload["error"])
            self.assert_no_running_steps(runtime, task.task_id)

    def test_runtime_passes_allowed_tools_to_model_adapter(self) -> None:
        """校验模型调用只收到对模型可见的工具定义。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果模型可见的工具模式未被传给模型。

        副作用:
            通过一个记录型模型适配器运行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            model = RecordingToolsModel()
            runtime = self._build_runtime(Path(temp_dir), model_adapter=model)
            task = runtime.create_task("inspect tools")

            asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(
                model.tool_names,
                ["list_directory", "read_file", "search_text"],
            )

    def test_agent_profile_filters_model_visible_tools(self) -> None:
        """校验 Agent 档案会收窄模型调用所见、调度器可见的工具。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果模型看到了 Agent 档案之外的工具。

        副作用:
            通过一个记录型模型适配器运行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            model = RecordingToolsModel()
            agent_profile = AgentProfile(
                agent_id="reader",
                role="reader",
                goal="Read one file only.",
                allowed_tools=("read_file",),
                context_policy="text_only_read_file",
            )
            runtime = self._build_runtime(
                Path(temp_dir),
                model_adapter=model,
                agent_profile=agent_profile,
            )
            task = runtime.create_task("inspect narrowed tools")

            asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(model.tool_names, ["read_file"])

    def test_agent_profile_denies_hidden_tool_execution(self) -> None:
        """校验被隐藏的、调度器可见工具无法通过伪造调用执行。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 Agent 档案之外的工具被执行。

        副作用:
            运行一个请求隐藏的、有副作用工具的模型。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            marker_path = project_root / "hidden-marker.txt"
            logger = logging.getLogger("test-agent-hidden-tool")
            logger.handlers = []
            logger.addHandler(logging.NullHandler())
            hidden_tool = ToolDefinition(
                name="hidden_write",
                description="Hidden side-effect tool.",
                permission="safe_read",
                required_params=("path",),
                handler=_write_test_handler,
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )
            scheduler = self._build_tool_scheduler(
                project_root,
                logger,
                extra_tools=(hidden_tool,),
            )
            agent_profile = AgentProfile(
                agent_id="reader",
                role="reader",
                goal="Only read files.",
                allowed_tools=("read_file",),
                context_policy="text_only_read_file",
            )
            runtime = self._build_runtime(
                project_root,
                model_adapter=HiddenToolModel(str(marker_path)),
                tool_error_limit=1,
                tool_scheduler=scheduler,
                agent_profile=agent_profile,
            )
            task = runtime.create_task("request hidden tool")

            events = asyncio.run(self._collect_events(runtime, task.task_id))
            tool_finished = [
                event for event in events if event.event_type == "tool_call_finished"
            ][0]

            self.assertEqual(runtime.get_task(task.task_id).status, "failed")
            self.assertEqual(tool_finished.payload["status"], "error")
            self.assertIn("agent does not allow tool", tool_finished.payload["error"])
            self.assertFalse(marker_path.exists())

    def test_agent_mismatch_fails_before_running_task(self) -> None:
        """校验运行时无法静默地执行另一个 Agent 的任务。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果错配的 Agent 执行被启动。

        副作用:
            创建一个任务并尝试用不同的运行时 Agent 运行它。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            store = SQLiteTaskStore(project_root / "app.sqlite3")
            reviewer = AgentProfile(
                agent_id="reviewer",
                role="reviewer",
                goal="Review code.",
                allowed_tools=("safe_read",),
                context_policy="text_only_review",
            )
            reviewer_runtime = self._build_runtime(
                project_root,
                task_store=store,
                agent_profile=reviewer,
            )
            task = reviewer_runtime.create_task("review later")
            developer_runtime = self._build_runtime(project_root, task_store=store)

            events = asyncio.run(self._collect_events(developer_runtime, task.task_id))

            self.assertEqual(events[-1].event_type, "run_failed")
            self.assertEqual(events[-1].payload["error"], "agent_profile_unavailable")
            self.assertEqual(events[-1].payload["task_agent_id"], "reviewer")
            self.assertEqual(developer_runtime.get_task(task.task_id).status, "failed")

    def test_runtime_preserves_tool_call_id_in_observation_context(self) -> None:
        """校验工具观测会与模型工具调用 id 配对。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果第二次模型调用缺少协议兼容的消息。

        副作用:
            创建一个临时的可读取项目文件并运行一个脚本化模型。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "note.txt").write_text("tool content", encoding="utf-8")
            model = ToolExchangeRecordingModel()
            runtime = self._build_runtime(project_root, model_adapter=model)
            task = runtime.create_task("read note")

            asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(model.second_call_messages[-2].role, "assistant")
            self.assertEqual(model.second_call_messages[-1].role, "tool")
            self.assertEqual(
                model.second_call_messages[-2].metadata["tool_call_id"],
                "call_read_1",
            )
            self.assertEqual(
                model.second_call_messages[-1].metadata["tool_call_id"],
                "call_read_1",
            )

    def test_running_cancel_closes_running_steps(self) -> None:
        """校验模型流式传输期间的取消会关闭活跃步骤。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果取消留下了运行中的步骤。

        副作用:
            运行一个在流式传输期间取消任务的模型适配器。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            model = CancellingModel()
            runtime = self._build_runtime(Path(temp_dir), model_adapter=model)
            task = runtime.create_task("cancel during run")
            model.runtime = runtime
            model.task_id = task.task_id

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(runtime.get_task(task.task_id).status, "cancelled")
            self.assertEqual(events[-1].event_type, "run_cancelled")
            self.assert_no_running_steps(runtime, task.task_id)

    def test_runtime_accepts_injected_workflow(self) -> None:
        """校验运行时将执行委托给注入的工作流策略。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果注入的工作流未被使用。

        副作用:
            通过一个测试工作流运行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            workflow = CompletingWorkflow()
            runtime = self._build_runtime(Path(temp_dir), workflow=workflow)
            task = runtime.create_task("custom workflow")

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertTrue(workflow.was_called)
            self.assertEqual(runtime.get_task(task.task_id).status, "completed")
            self.assertEqual(events[-1].event_type, "run_finished")

    def test_react_workflow_uses_step_controller(self) -> None:
        """校验 ReAct 风格工作流会委托步骤继续决定。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果工作流忽略了注入的控制器。

        副作用:
            通过一个带有阻塞控制器的的工作流运行一个任务。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = BlockingStepController()
            workflow = ReactLikeWorkflow(step_controller=controller)
            runtime = self._build_runtime(
                Path(temp_dir),
                model_adapter=FailingModel(),
                workflow=workflow,
            )
            task = runtime.create_task("blocked by controller")

            events = asyncio.run(self._collect_events(runtime, task.task_id))

            self.assertEqual(controller.calls, 1)
            self.assertEqual(runtime.get_task(task.task_id).status, "failed")
            self.assertEqual(events[-1].payload["error"], "max_steps_reached")
            self.assertEqual(runtime.list_steps(task.task_id), [])

    async def _collect_events(self, runtime: AgentRuntime, task_id: str) -> list:
        """收集一个运行时任务发出的所有事件。

        参数:
            runtime: 被测的 Agent 运行时。
            task_id: 待执行的任务标识符。

        返回:
            已发出的运行时事件的有序列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            通过运行时执行该任务。
        """

        return [event async for event in runtime.run_task(task_id)]

    def assert_no_running_steps(self, runtime: AgentRuntime, task_id: str) -> None:
        """断言一个任务没有持久化的运行中步骤。

        参数:
            runtime: 需要检查其存储的运行时。
            task_id: 需要检查其步骤的任务标识符。

        返回:
            无。

        异常:
            AssertionError: 如果任何步骤仍处于 running 状态。

        副作用:
            读取运行时步骤记录。
        """

        running_steps = [
            step for step in runtime.list_steps(task_id) if step.status == "running"
        ]
        self.assertEqual(running_steps, [])

    def _build_runtime(
        self,
        temp_dir: Path,
        model_adapter=None,
        tool_error_limit: int = 3,
        max_steps: int = 8,
        max_context_chars: int = 20000,
        workflow=None,
        task_store=None,
        tool_scheduler=None,
        agent_profile=None,
    ) -> AgentRuntime:
        """构建一个带有隔离 SQLite 存储与临时日志的运行时。

        参数:
            temp_dir: 用作项目根目录与日志父目录的临时目录。
            model_adapter: 可选的流式模型适配器覆盖。
            tool_error_limit: 运行时失败前允许的最大工具错误数。
            max_steps: 运行时失败前允许的最大模型步骤数。
            max_context_chars: 模型调用前允许的最大字符数预算。
            workflow: 可选的工作流策略覆盖。
            task_store: 可选的存储覆盖。
            tool_scheduler: 可选的工具调度器覆盖。
            agent_profile: 可选的 Agent 档案覆盖。

        返回:
            为单元测试配置好的 AgentRuntime。

        异常:
            无。

        副作用:
            创建一个没有外部处理器的日志记录器实例。
        """

        logger = logging.getLogger(f"test-runtime-{id(temp_dir)}")
        logger.handlers = []
        logger.addHandler(logging.NullHandler())
        return AgentRuntime(
            settings=BackendSettings(
                project_root=temp_dir,
                log_file=temp_dir / "app.log",
                database_file=temp_dir / "app.sqlite3",
                max_steps=max_steps,
                tool_error_limit=tool_error_limit,
                max_context_chars=max_context_chars,
            ),
            task_store=task_store or SQLiteTaskStore(temp_dir / "app.sqlite3"),
            context_builder=TextContextBuilder(),
            model_adapter=model_adapter or EchoStreamingModelAdapter(),
            tool_scheduler=tool_scheduler or self._build_tool_scheduler(temp_dir, logger),
            logger=logger,
            agent_profile=agent_profile,
            workflow=workflow,
        )

    def _build_tool_scheduler(
        self,
        project_root: Path,
        logger: logging.Logger,
        extra_tools: tuple = (),
        approval_required_permissions: tuple = (),
    ) -> ToolScheduler:
        """为运行时测试构建一个安全只读工具调度器。

        参数:
            project_root: 安全只读工具使用的临时根目录。
            logger: 与运行时共享的日志记录器。
            extra_tools: 需要注册的额外测试工具定义。
            approval_required_permissions: 需要审批的权限级别。

        返回:
            配置了安全只读工具的 ToolScheduler。

        异常:
            OSError: 如果项目根目录无法被解析。

        副作用:
            创建内存中的工具注册表对象。
        """

        safe_tools = SafeReadTools(project_root)
        registry = ToolRegistry(tuple(safe_tools.definitions()) + tuple(extra_tools))
        return ToolScheduler(
            registry=registry,
            allowed_permissions=("safe_read",),
            logger=logger,
            approval_required_permissions=approval_required_permissions,
        )


class ScriptedToolModel:
    """在第一个模型步骤脚本化一个工具调用，在第二个步骤脚本化最终文本。"""

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """先发出一个 read_file 工具调用，待观测结果存在后再结束。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            表示工具调用或最终答案的 ModelDelta 值。

        异常:
            无。

        副作用:
            无。
        """

        if not any(message.role == "tool" for message in messages):
            yield ModelDelta(
                text="",
                tool_call=ToolCall(
                    tool_name="read_file",
                    arguments={"path": "note.txt"},
                ),
            )
            return
        yield ModelDelta(text="read complete", is_final=True)


class RecordingContextModel:
    """记录模型适配器收到的运行时消息。"""

    def __init__(self) -> None:
        """初始化被记录的消息存储。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化可变的消息存储。
        """

        self.messages = []

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """记录模型调用消息并发出一个最终响应。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            一个最终响应增量。

        异常:
            无。

        副作用:
            保存一份运行时消息的副本。
        """

        self.messages = list(messages)
        yield ModelDelta(text="context recorded", is_final=True)


class HiddenToolModel:
    """脚本化一个被 Agent 档案隐藏的工具的调用。"""

    def __init__(self, path: str) -> None:
        """初始化隐藏工具请求路径。

        参数:
            path: 传给隐藏工具的文件系统路径。

        返回:
            无。

        异常:
            无。

        副作用:
            为脚本化模型调用保存该路径。
        """

        self._path = path

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """发出一个隐藏工具调用。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            一个包含隐藏工具请求的模型增量。

        异常:
            无。

        副作用:
            无。
        """

        yield ModelDelta(
            text="",
            tool_call=ToolCall(
                tool_name="hidden_write",
                arguments={"path": self._path},
                call_id="call_hidden_1",
            ),
        )


class ToolExchangeRecordingModel:
    """脚本化一个工具调用并记录后续的上下文消息。"""

    def __init__(self) -> None:
        """初始化被记录的消息状态。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化可变的测试状态。
        """

        self.second_call_messages = []

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """发出一个服务商风格的工具调用，然后在观测之后结束。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            第一次调用时发出一个工具调用，然后发出一个最终响应。

        异常:
            无。

        副作用:
            保存第二次调用的消息供断言使用。
        """

        if not any(message.role == "tool" for message in messages):
            yield ModelDelta(
                text="",
                tool_call=ToolCall(
                    tool_name="read_file",
                    arguments={"path": "note.txt"},
                    call_id="call_read_1",
                ),
            )
            return
        self.second_call_messages = list(messages)
        yield ModelDelta(text="read complete", is_final=True)


class ApprovalRequiredToolModel:
    """脚本化一个需要用户审批的写工具调用。"""

    def __init__(self, path: str) -> None:
        """初始化被请求的写路径。

        参数:
            path: 模型将请求工具去写的文件系统路径。

        返回:
            无。

        异常:
            无。

        副作用:
            为脚本化工具调用保存该路径。
        """

        self._path = path

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """发出一个 write_file 工具调用。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            一个包含写工具请求的模型增量。

        异常:
            无。

        副作用:
            无。
        """

        yield ModelDelta(
            text="",
            tool_call=ToolCall(
                tool_name="write_file",
                arguments={"path": self._path},
                call_id="call_write_1",
            ),
        )


class ScriptedFailingToolModel:
    """每当运行时询问模型时就脚本化一个未知工具调用。"""

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """为每个模型步骤发出一个缺失的工具调用。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            包含未知工具调用的 ModelDelta。

        异常:
            无。

        副作用:
            无。
        """

        yield ModelDelta(
            text="",
            tool_call=ToolCall(
                tool_name="missing_tool",
                arguments={},
            ),
        )


class EmptyModel:
    """脚本化一个没有有用输出就结束的模型流。"""

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """不发出任何增量。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            无。

        异常:
            无。

        副作用:
            无。
        """

        if False:
            yield ModelDelta(text="")


class FailingModel:
    """脚本化一个在流式传输期间抛出的模型适配器。"""

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """抛出一个模型失败。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            无。

        异常:
            RuntimeError: 始终抛出以模拟服务商失败。

        副作用:
            无。
        """

        raise RuntimeError("model exploded")
        if False:
            yield ModelDelta(text="")


class CountingModel:
    """脚本化一个记录流式是否启动的模型适配器。"""

    def __init__(self) -> None:
        """初始化调用计数器。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化可变的测试状态。
        """

        self.calls = 0

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """递增调用计数器并发出一个最终响应。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            一个最终响应增量。

        异常:
            无。

        副作用:
            递增 ``calls``。
        """

        self.calls += 1
        yield ModelDelta(text="should not run", is_final=True)


class RecordingToolsModel:
    """脚本化一个记录所暴露工具名的模型适配器。"""

    def __init__(self) -> None:
        """初始化工具记录状态。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化可变的测试状态。
        """

        self.tool_names = []

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """记录面向模型的工具并完成响应。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            一个最终的模型增量。

        异常:
            无。

        副作用:
            将排序后的工具名保存到 ``tool_names``。
        """

        self.tool_names = [tool.name for tool in tools or []]
        yield ModelDelta(text="tools recorded", is_final=True)


class CancellingModel:
    """脚本化一个在流式传输期间取消任务的模型适配器。"""

    def __init__(self) -> None:
        """初始化延迟的运行时引用。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化可变的测试引用。
        """

        self.runtime = None
        self.task_id = ""

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: List[ModelToolDefinition] = None,
    ) -> AsyncIterator[ModelDelta]:
        """取消当前任务然后发出一个增量。

        参数:
            messages: 运行时累积的运行时消息。
            tools: 本次模型调用可用的、面向模型的工具定义。

        生成:
            取消请求之后的一个模型增量。

        异常:
            AssertionError: 如果运行时或任务 id 未配置。

        副作用:
            通过运行时取消该任务。
        """

        assert self.runtime is not None
        assert self.task_id
        self.runtime.cancel_task(self.task_id)
        yield ModelDelta(text="late")


class BlockingStepController:
    """在第一个模型步骤之前阻塞工作流的测试控制器。"""

    def __init__(self) -> None:
        """初始化调用追踪。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化一个内存中的计数器。
        """

        self.calls = 0

    async def next_step(self, step_count: int, max_steps: int) -> StepDecision:
        """返回一个阻止模型执行的决定。

        参数:
            step_count: 当前的模型步骤数。
            max_steps: 最大的模型步骤数。

        返回:
            停止工作流的 StepDecision。

        异常:
            无。

        副作用:
            递增调用计数。
        """

        self.calls += 1
        return StepDecision(step_count=step_count, can_continue=False)


class CompletingWorkflow:
    """一个不调用模型就完成任务测试工作流。"""

    def __init__(self) -> None:
        """初始化工作流调用追踪。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化一个内存中的标志。
        """

        self.was_called = False

    async def run(self, task: TaskRecord, operations: RuntimeOperations) -> AsyncIterator:
        """使用运行时操作完成任务。

        参数:
            task: 正在执行的任务。
            operations: 运行时操作门面。

        生成:
            证明自定义工作流已运行的运行时事件。

        异常:
            无。

        副作用:
            更新任务状态并记录一个运行时事件。
        """

        self.was_called = True
        operations.update_task_status(task.task_id, "completed")
        yield operations.record_event(
            EventType.RUN_FINISHED,
            task.task_id,
            {"status": "completed", "workflow": "custom"},
        )


class FailingCheckpointStore(SQLiteTaskStore):
    """拒绝检查点写入的 SQLite 存储变体，用于测试。"""

    def create_checkpoint(
        self,
        task_id: str,
        stage: str,
        summary: str,
        snapshot: dict,
    ):
        """抛出一个检查点持久化失败。

        参数:
            task_id: 与检查点关联的任务标识符。
            stage: 产出该检查点的运行时阶段。
            summary: 检查点摘要。
            snapshot: 检查点快照。

        返回:
            从不返回。

        异常:
            RuntimeError: 始终抛出以模拟检查点存储失败。

        副作用:
            无。
        """

        raise RuntimeError("checkpoint storage unavailable")


class FailingCheckpointAndFailureEventStore(FailingCheckpointStore):
    """同时拒绝持久化检查点失败事件的存储变体。"""

    def append_event(self, event) -> None:
        """追加事件，但检查点失败事件除外。

        参数:
            event: 待持久化的运行时事件。

        返回:
            无。

        异常:
            RuntimeError: 如果事件是 ``checkpoint_failed``。

        副作用:
            通过基类存储持久化非检查点失败事件。
        """

        if event.event_type == "checkpoint_failed":
            raise RuntimeError("event storage unavailable")
        super().append_event(event)


if __name__ == "__main__":
    unittest.main()

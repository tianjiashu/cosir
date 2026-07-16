"""Agent Replay 投影与服务测试。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from app.core.replay.projector import ReplayProjector
from app.core.replay.service import ReplayService
from app.core.trace.records import TraceEventRecord
from app.storage.trace_store import TraceStore


class AgentReplayTests(unittest.TestCase):
    """验证 Agent Replay 从 trace ledger 构建只读 timeline。"""

    def test_projector_groups_model_tool_and_runtime_events(self) -> None:
        """校验投影器会聚合模型、工具、审批、恢复、checkpoint 和失败事件。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果节点类型、状态或关联事件不符合预期。

        副作用:
            无。
        """

        events = [
            _event(1, "run_created", {"status": "pending"}),
            _event(2, "model_requested", {"model": "echo"}),
            _event(3, "model_delta", {"text": "hel"}, span_id="span-model"),
            _event(4, "model_delta", {"text": "lo"}, span_id="span-model"),
            _event(5, "model_completed", {"status": "completed"}, span_id="span-model"),
            _event(6, "tool_call_created", {"tool_call_id": "tool-1", "tool_name": "read_file"}),
            _event(7, "tool_execution_started", {"tool_call_id": "tool-1", "tool_name": "read_file"}),
            _event(8, "tool_execution_completed", {"tool_call_id": "tool-1", "tool_name": "read_file", "summary": "ok"}),
            _event(9, "approval_requested", {"approval_id": "approval-1"}),
            _event(10, "approval_decided", {"approval_id": "approval-1", "decision": "approved"}),
            _event(11, "resume_started", {"resume_command_id": "resume-1"}),
            _event(12, "resume_completed", {"resume_command_id": "resume-1"}),
            _event(13, "checkpoint_created", {"checkpoint_id": "checkpoint-1"}),
            _event(14, "run_failed", {"error": "boom"}),
        ]

        timeline = ReplayProjector().project_events(reversed(events))

        node_types = [node.node_type for node in timeline.nodes]
        self.assertEqual(timeline.trace_id, "trace-1")
        self.assertEqual(timeline.run_id, "run-1")
        self.assertIn("model_call", node_types)
        self.assertIn("model_output", node_types)
        self.assertIn("tool_call", node_types)
        self.assertIn("tool_result", node_types)
        self.assertIn("approval_wait", node_types)
        self.assertIn("approval_decision", node_types)
        self.assertIn("resume", node_types)
        self.assertIn("checkpoint", node_types)
        self.assertIn("run_failed", node_types)
        model_output = _first_node(timeline.nodes, "model_output")
        self.assertEqual(model_output.payload["text"], "hello")
        self.assertEqual(model_output.related_event_ids, [events[1].event_id, events[2].event_id, events[3].event_id, events[4].event_id])
        tool_result = _first_node(timeline.nodes, "tool_result")
        self.assertEqual(tool_result.status, "completed")
        self.assertEqual(tool_result.summary, "ok")
        self.assertEqual(_first_node(timeline.nodes, "checkpoint").checkpoint_id, "checkpoint-1")
        approval_wait = _first_node(timeline.nodes, "approval_wait")
        approval_decision = _first_node(timeline.nodes, "approval_decision")
        self.assertEqual(approval_wait.payload["approval_chain_event_types"], ["approval_requested", "approval_decided"])
        self.assertEqual(approval_decision.payload["approval_chain_event_types"], ["approval_requested", "approval_decided"])
        failed = _first_node(timeline.nodes, "run_failed")
        self.assertEqual(failed.status, "failed")

    def test_projector_marks_tool_failure_cancel_and_timeout(self) -> None:
        """校验工具异常终态会映射到稳定 replay 状态。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果工具失败、取消或超时状态映射错误。

        副作用:
            无。
        """

        events = [
            _event(1, "tool_call_created", {"tool_call_id": "failed", "tool_name": "shell"}),
            _event(2, "tool_execution_failed", {"tool_call_id": "failed", "tool_name": "shell", "error": "bad"}),
            _event(3, "tool_call_created", {"tool_call_id": "cancelled", "tool_name": "shell"}),
            _event(4, "tool_execution_cancelled", {"tool_call_id": "cancelled", "tool_name": "shell"}),
            _event(5, "tool_call_created", {"tool_call_id": "timeout", "tool_name": "shell"}),
            _event(6, "tool_execution_timed_out", {"tool_call_id": "timeout", "tool_name": "shell"}),
        ]

        timeline = ReplayProjector().project_events(events)
        tool_calls = [node for node in timeline.nodes if node.node_type == "tool_call"]

        self.assertEqual([node.status for node in tool_calls], ["failed", "cancelled", "timed_out"])

    def test_projector_splits_multiple_model_calls_and_supports_delta_payload(self) -> None:
        """校验多次模型调用不会被压成一个 replay 节点。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果模型调用分段、delta 字段或失败状态处理错误。

        副作用:
            无。
        """

        events = [
            _event(1, "model_requested", {"step_id": "step-1"}),
            _event(2, "model_delta", {"delta": "first"}),
            _event(3, "model_completed", {"step_id": "step-1"}),
            _event(4, "model_requested", {"step_id": "step-2"}),
            _event(5, "model_delta", {"delta": "second"}),
            _event(6, "model_failed", {"step_id": "step-2", "error": "model error"}),
        ]

        timeline = ReplayProjector().project_events(events)
        model_outputs = [node for node in timeline.nodes if node.node_type == "model_output"]
        model_calls = [node for node in timeline.nodes if node.node_type == "model_call"]

        self.assertEqual([node.payload["text"] for node in model_outputs], ["first", "second"])
        self.assertEqual([node.status for node in model_calls], ["completed", "failed"])
        self.assertEqual(model_outputs[0].sequence_start, 1)
        self.assertEqual(model_outputs[1].sequence_start, 4)

    def test_projector_covers_planned_running_result_and_failed_checkpoint_resume(self) -> None:
        """校验工具中间态、checkpoint 失败和 resume 失败会进入 replay。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果边界事件缺失或状态错误。

        副作用:
            无。
        """

        events = [
            _event(1, "tool_call_created", {"tool_call_id": "planned", "tool_name": "shell"}),
            _event(2, "tool_execution_started", {"tool_call_id": "running", "tool_name": "shell"}),
            _event(3, "tool_execution_failed", {"tool_call_id": "running", "tool_name": "shell", "error": "bad"}),
            _event(4, "checkpoint_failed", {"stage": "run_failed", "error": "checkpoint bad"}),
            _event(5, "approval_decided", {"approval_id": "approval-retry", "resume_command_id": "resume-retry", "decision": "approved"}),
            _event(6, "resume_started", {"resume_command_id": "resume-retry"}),
            _event(7, "resume_failed", {"resume_command_id": "resume-retry", "error": "temporary"}),
            _event(8, "resume_started", {"resume_command_id": "resume-retry"}),
            _event(9, "resume_completed", {"resume_command_id": "resume-retry"}),
            _event(10, "recovery_reconciled", {"status": "resuming"}),
        ]

        timeline = ReplayProjector().project_events(events)
        tool_calls = [node for node in timeline.nodes if node.node_type == "tool_call"]
        tool_result = _first_node(timeline.nodes, "tool_result")
        checkpoint = _first_node(timeline.nodes, "checkpoint")
        resume = _first_node(timeline.nodes, "resume")

        self.assertEqual([node.status for node in tool_calls], ["planned", "failed"])
        self.assertEqual(tool_result.status, "failed")
        self.assertEqual(tool_result.summary, "bad")
        self.assertEqual(checkpoint.status, "failed")
        self.assertEqual(resume.status, "completed")
        self.assertEqual(resume.payload["approval_id"], "approval-retry")
        self.assertEqual(resume.payload["failed_attempt_count"], 1)
        self.assertEqual(len(resume.related_event_ids), 5)
        self.assertTrue(any(node.payload.get("status") == "resuming" for node in timeline.nodes if node.node_type == "resume"))

    def test_projector_extracts_artifact_checkpoint_and_debug_from_trace_payload(self) -> None:
        """校验 replay 只从 trace payload 提取 artifact、checkpoint 和 debug 引用。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果关联字段或 debug 信息缺失。

        副作用:
            无。
        """

        events = [
            _event(1, "tool_call_created", {"tool_call_id": "tool-1", "tool_name": "shell"}),
            _event(
                2,
                "tool_execution_completed",
                {
                    "tool_call_id": "tool-1",
                    "tool_name": "shell",
                    "result": {"artifact_id": "artifact-1"},
                    "artifact_ids": ["artifact-2"],
                },
            ),
            _event(3, "checkpoint_created", {"checkpoint_id": "checkpoint-1"}),
        ]

        timeline = ReplayProjector().project_events(events)
        tool_result = _first_node(timeline.nodes, "tool_result")
        checkpoint = _first_node(timeline.nodes, "checkpoint")

        self.assertEqual(set(tool_result.related_artifact_ids), {"artifact-1", "artifact-2"})
        self.assertEqual(tool_result.debug["tool_call_ids"], ["tool-1"])
        self.assertEqual(checkpoint.checkpoint_id, "checkpoint-1")
        self.assertEqual(timeline.debug["event_count"], 3)

    def test_service_returns_run_trace_and_node_detail_without_cache(self) -> None:
        """校验 ReplayService 基于 trace_store 即时投影并返回节点详情。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 run、trace 或 node 查询结果错误。

        副作用:
            创建临时 SQLite 数据库。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            store = TraceStore(Path(temp_dir) / "app.sqlite3")
            first = store.append_event(_event(1, "run_created", {"status": "pending"}))
            store.append_event(_event(2, "run_completed", {"status": "completed"}))
            service = ReplayService(store)

            run_replay = service.get_run_replay("run-1")
            trace_replay = service.get_trace_replay("trace-1")
            detail = service.get_node_detail(run_replay["nodes"][0]["replay_node_id"])

            self.assertEqual(run_replay["run_id"], "run-1")
            self.assertEqual(trace_replay["trace_id"], "trace-1")
            self.assertEqual(detail["related_event_ids"], [first.event_id])
            self.assertIn("payload", detail)
            self.assertIn("debug", detail)

    def test_service_can_return_debug_and_compacts_timeline_summary(self) -> None:
        """校验 ReplayService 支持 debug 开关并压缩列表摘要。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 debug 或摘要压缩行为不符合契约。

        副作用:
            创建临时 SQLite 数据库。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            store = TraceStore(Path(temp_dir) / "app.sqlite3")
            store.append_event(_event(1, "run_created", {"summary": "x" * 260}))
            service = ReplayService(store)

            replay = service.get_run_replay("run-1", include_debug=True)

            self.assertIn("debug", replay)
            self.assertIn("debug", replay["nodes"][0])
            self.assertLessEqual(len(replay["nodes"][0]["summary"]), 200)

    def test_service_rejects_invalid_or_missing_identifiers(self) -> None:
        """校验 ReplayService 对非法输入和不存在数据给出明确错误。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果错误类型不符合服务契约。

        副作用:
            创建临时 SQLite 数据库。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ReplayService(TraceStore(Path(temp_dir) / "app.sqlite3"))

            with self.assertRaises(ValueError):
                service.get_run_replay(" ")
            with self.assertRaises(KeyError):
                service.get_run_replay("missing-run")
            with self.assertRaises(ValueError):
                service.get_node_detail("bad-node-id")


def _event(sequence_no: int, event_type: str, payload: dict, span_id: str = "") -> TraceEventRecord:
    """构造测试用 trace event。

    参数:
        sequence_no: 同一 run 内的事件序号。
        event_type: trace event 类型。
        payload: 事件载荷。
        span_id: 可选 span 标识。

    返回:
        TraceEventRecord。

    异常:
        无。

    副作用:
        生成默认 event_id。
    """

    return TraceEventRecord(
        trace_id="trace-1",
        run_id="run-1",
        task_id="task-1",
        event_type=event_type,
        source="test",
        payload=payload,
        sequence_no=sequence_no,
        span_id=span_id,
        created_at=datetime(2026, 7, 15, tzinfo=timezone.utc) + timedelta(milliseconds=sequence_no),
    )


def _first_node(nodes, node_type: str):
    """返回第一个指定类型的 replay 节点。

    参数:
        nodes: replay 节点列表。
        node_type: 目标节点类型。

    返回:
        匹配的节点。

    异常:
        AssertionError: 如果节点不存在。

    副作用:
        无。
    """

    for node in nodes:
        if node.node_type == node_type:
            return node
    raise AssertionError(f"missing replay node type: {node_type}")


if __name__ == "__main__":
    unittest.main()

"""针对任务创建与 SSE 流式传输的 FastAPI API 测试。"""

from contextlib import contextmanager
from datetime import date
import importlib.util
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from app.domain.approvals.service import ApprovalService
from app.domain.approvals.store import ApprovalStore
from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.core.replay.service import ReplayService
from app.core.trace.query_service import TraceQueryService
from app.core.trace.recorder import TraceRecorder
from app.config.logging import current_log_file, dated_log_file
from app.models.base import ModelDelta, ModelToolDefinition, RuntimeMessage
from app.models.echo import EchoStreamingModelAdapter
from app.core.runs.recovery import RecoveryManager
from app.core.runs.resume import ResumeDispatcher
from app.core.runs.store import DurableRunStore
from app.core.runtime.runner import AgentRuntime
from app.storage.task_store import SQLiteTaskStore
from app.storage.trace_store import TraceStore
from app.tools.types import ArtifactRequest, ToolCall, ToolDefinition
from app.tools.registry.memory import ToolRegistry
from app.tools.builtin.safe_read import SafeReadTools
from app.tools.runtime.compatibility import ToolScheduler


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

    def test_run_trace_endpoint_reads_trace_events_and_jsonl_logs(self) -> None:
        """校验 run trace API 返回 ledger 事件并解析 JSONL 文件日志。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 trace API 未返回事件或日志。

        副作用:
            创建任务、运行 SSE，并写入临时 JSONL 日志。
        """

        client = self._build_client()
        create_response = client.post("/tasks", json={"text": "trace api"})
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]

        stream_response = client.get(f"/tasks/{task_id}/stream")
        self.assertEqual(stream_response.status_code, 200)
        run_id = client.app.state.runtime_for_tests._run_store.get_by_task(task_id).run_id

        response = client.get(f"/runs/{run_id}/trace")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run_id"], run_id)
        self.assertTrue(payload["trace_id"])
        self.assertTrue(any(event["event_type"] == "run_started" for event in payload["events"]))
        self.assertTrue(any(row.get("data", {}).get("run_id") == run_id for row in payload["logs"]))
        self.assertTrue(any(row["trace_id"] == payload["trace_id"] for row in payload["logs"]))
        self.assertEqual(
            [event["sequence_no"] for event in payload["events"]],
            sorted(event["sequence_no"] for event in payload["events"]),
        )
        traces_response = client.get("/traces")
        detail_response = client.get(f"/traces/{payload['trace_id']}")

        self.assertEqual(traces_response.status_code, 200)
        self.assertTrue(any(item["trace_id"] == payload["trace_id"] for item in traces_response.json()))
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()["trace_id"], payload["trace_id"])
        self.assertTrue(detail_response.json()["events"])
        self.assertFalse(_sqlite_table_exists(client.app.state.project_root_for_tests / "app.sqlite3", "trace_logs"))

    def test_replay_endpoints_return_timeline_and_node_detail(self) -> None:
        """校验 Replay API 返回 run/trace timeline 和节点详情。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 replay API 不可用或详情缺少 payload。

        副作用:
            创建任务、运行 SSE，并读取 trace ledger。
        """

        client = self._build_client()
        create_response = client.post("/tasks", json={"text": "replay api"})
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]
        self.assertEqual(client.get(f"/tasks/{task_id}/stream").status_code, 200)
        run_id = client.app.state.runtime_for_tests._run_store.get_by_task(task_id).run_id

        run_response = client.get(f"/runs/{run_id}/replay")

        self.assertEqual(run_response.status_code, 200)
        run_payload = run_response.json()
        self.assertEqual(run_payload["run_id"], run_id)
        self.assertTrue(run_payload["trace_id"])
        self.assertTrue(run_payload["nodes"])
        self.assertTrue(any(node["node_type"] == "run_started" for node in run_payload["nodes"]))
        self.assertTrue(any(node["node_type"] == "model_output" for node in run_payload["nodes"]))
        self.assertTrue(all("payload" not in node for node in run_payload["nodes"]))
        debug_response = client.get(f"/runs/{run_id}/replay?include_debug=true")
        trace_response = client.get(f"/traces/{run_payload['trace_id']}/replay")
        node_id = run_payload["nodes"][0]["replay_node_id"]
        node_response = client.get(f"/replay/nodes/{node_id}")

        self.assertEqual(debug_response.status_code, 200)
        self.assertIn("debug", debug_response.json())
        self.assertIn("debug", debug_response.json()["nodes"][0])
        self.assertEqual(trace_response.status_code, 200)
        self.assertEqual(trace_response.json()["trace_id"], run_payload["trace_id"])
        self.assertEqual(node_response.status_code, 200)
        node_payload = node_response.json()
        self.assertEqual(node_payload["replay_node_id"], node_id)
        self.assertLessEqual(node_payload["sequence_start"], node_payload["sequence_end"])
        self.assertIn("related_event_ids", node_payload)
        self.assertIn("related_span_ids", node_payload)
        self.assertIn("related_artifact_ids", node_payload)
        self.assertIn("duration_ms", node_payload)
        self.assertIn("payload", node_payload)

    def test_run_replay_endpoint_is_strictly_read_only(self) -> None:
        """校验 Replay 查询不会消费 pending 或 processing 恢复命令。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 replay 查询改变了恢复命令状态。

        副作用:
            创建一个等待中的 run 和 pending resume command，并调用 replay API。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        runtime, run_store, _approval_service = self._build_runtime_with_approvals()
        task = runtime.create_task("replay read only")
        run = run_store.get_by_task(task.task_id)
        self.assertIsNotNone(run)
        run_store.mark_status(run.run_id, "waiting", wait_reason="approval")
        created = run_store.create_resume_command(
            run.run_id,
            "approve_tool",
            {"approval_id": "approval-pending"},
            "replay-query-key",
        )
        client = TestClient(create_app(runtime=runtime))

        response = client.get(f"/runs/{run.run_id}/replay")
        trace_id = response.json()["trace_id"]
        trace_response = client.get(f"/traces/{trace_id}/replay")
        node_response = client.get(f"/replay/nodes/{response.json()['nodes'][0]['replay_node_id']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(trace_response.status_code, 200)
        self.assertEqual(node_response.status_code, 200)
        self.assertEqual(run_store.get_resume_command_by_key(created.idempotency_key).status, "pending")

    def test_replay_records_approval_decision_and_resume_nodes(self) -> None:
        """校验审批决策和恢复消费会写入可回放 trace 节点。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果真实审批恢复链路没有进入 replay。

        副作用:
            创建审批请求，通过 API 决策并读取 replay。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        runtime, run_store, approval_service = self._build_runtime_with_approvals()
        task = runtime.create_task("approval replay")
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

        decision_response = client.post(
            f"/approvals/{approval.approval_id}/decision",
            json={"decision": "denied", "reason": "no", "idempotency_key": "replay-decision-key"},
        )
        replay_response = client.get(f"/runs/{run.run_id}/replay")

        self.assertEqual(decision_response.status_code, 200)
        self.assertEqual(replay_response.status_code, 200)
        node_types = [node["node_type"] for node in replay_response.json()["nodes"]]
        self.assertIn("approval_wait", node_types)
        self.assertIn("approval_decision", node_types)
        self.assertIn("resume", node_types)

    def test_replay_uses_persisted_tool_call_id_from_real_tool_runtime(self) -> None:
        """校验真实 ToolRuntime 链路会把持久化 tool_call_id 写入 replay。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 replay 工具节点没有使用工具事实表中的 tool_call_id。

        副作用:
            创建任务、执行真实 safe_read 工具、读取 SQLite 工具事实和 replay API。
        """

        client = self._build_tool_runtime_client()
        project_root = client.app.state.project_root_for_tests
        (project_root / "note.txt").write_text("real tool content", encoding="utf-8")
        create_response = client.post("/tasks", json={"text": "read note"})
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]

        stream_response = client.get(f"/tasks/{task_id}/stream")
        run_id = client.app.state.runtime_for_tests._run_store.get_by_task(task_id).run_id
        replay_response = client.get(f"/runs/{run_id}/replay")
        persisted_tool_call_ids = _sqlite_tool_call_ids(project_root / "app.sqlite3")

        self.assertEqual(stream_response.status_code, 200)
        self.assertEqual(replay_response.status_code, 200)
        self.assertEqual(len(persisted_tool_call_ids), 1)
        tool_nodes = [node for node in replay_response.json()["nodes"] if node["node_type"] in {"tool_call", "tool_result"}]
        self.assertEqual(len(tool_nodes), 2)
        node_details = [client.get(f"/replay/nodes/{node['replay_node_id']}").json() for node in tool_nodes]
        self.assertTrue(all(detail["payload"]["tool_call_id"] == persisted_tool_call_ids[0] for detail in node_details))

    def test_replay_splits_multiple_real_tool_calls_in_one_model_response(self) -> None:
        """校验同一模型响应中的多个真实工具调用会形成独立 replay 节点。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果多个工具调用串线或未使用持久化 tool_call_id。

        副作用:
            创建任务、执行两个真实 safe_read 工具并读取 replay API。
        """

        client = self._build_tool_runtime_client(model_adapter=MultiReplayToolModel())
        project_root = client.app.state.project_root_for_tests
        (project_root / "note-a.txt").write_text("a", encoding="utf-8")
        (project_root / "note-b.txt").write_text("b", encoding="utf-8")
        create_response = client.post("/tasks", json={"text": "read two notes"})
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]

        stream_response = client.get(f"/tasks/{task_id}/stream")
        run_id = client.app.state.runtime_for_tests._run_store.get_by_task(task_id).run_id
        replay_response = client.get(f"/runs/{run_id}/replay")
        persisted_tool_call_ids = _sqlite_tool_call_ids(project_root / "app.sqlite3")

        self.assertEqual(stream_response.status_code, 200)
        self.assertEqual(replay_response.status_code, 200)
        self.assertEqual(len(persisted_tool_call_ids), 2)
        tool_nodes = [node for node in replay_response.json()["nodes"] if node["node_type"] == "tool_call"]
        result_nodes = [node for node in replay_response.json()["nodes"] if node["node_type"] == "tool_result"]
        self.assertEqual(len(tool_nodes), 2)
        self.assertEqual(len(result_nodes), 2)
        details = [client.get(f"/replay/nodes/{node['replay_node_id']}").json() for node in tool_nodes + result_nodes]
        detail_tool_call_ids = {detail["payload"]["tool_call_id"] for detail in details}
        tool_group_ids = {detail["payload"].get("tool_group_id") for detail in details}
        self.assertEqual(detail_tool_call_ids, set(persisted_tool_call_ids))
        self.assertEqual(len(tool_group_ids), 1)

    def test_replay_detail_links_real_artifact_from_tool_runtime(self) -> None:
        """校验真实工具产物会通过 trace payload 进入 replay 详情。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 artifact_id 没有进入 replay 节点关联字段。

        副作用:
            执行一个会产生 ArtifactRequest 的真实工具并写入 artifact 文件。
        """

        artifact_tool = ToolDefinition(
            name="large_output",
            description="Return a large captured output artifact.",
            permission="safe_read",
            required_params=(),
            handler=_artifact_handler,
            parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )
        client = self._build_tool_runtime_client(
            model_adapter=ArtifactReplayToolModel(),
            extra_tools=(artifact_tool,),
        )
        create_response = client.post("/tasks", json={"text": "capture output"})
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]

        stream_response = client.get(f"/tasks/{task_id}/stream")
        run_id = client.app.state.runtime_for_tests._run_store.get_by_task(task_id).run_id
        replay_response = client.get(f"/runs/{run_id}/replay")

        self.assertEqual(stream_response.status_code, 200)
        self.assertEqual(replay_response.status_code, 200)
        result_nodes = [node for node in replay_response.json()["nodes"] if node["node_type"] == "tool_result"]
        self.assertEqual(len(result_nodes), 1)
        detail = client.get(f"/replay/nodes/{result_nodes[0]['replay_node_id']}").json()
        self.assertTrue(detail["related_artifact_ids"])
        self.assertEqual(detail["related_artifact_ids"], [detail["payload"]["result"]["artifact_id"]])

    def test_trace_logs_endpoint_reads_jsonl_without_trace_log_table(self) -> None:
        """校验 trace logs API 直接读取 JSONL，并支持 level 过滤。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 API 依赖 trace_logs 表或未执行 level 过滤。

        副作用:
            创建任务、运行 SSE，并通过 API 读取 JSONL 日志。
        """

        client = self._build_client()
        create_response = client.post("/tasks", json={"text": "trace logs api"})
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]

        stream_response = client.get(f"/tasks/{task_id}/stream")
        self.assertEqual(stream_response.status_code, 200)
        run_id = client.app.state.runtime_for_tests._run_store.get_by_task(task_id).run_id
        trace_response = client.get(f"/runs/{run_id}/trace")
        self.assertEqual(trace_response.status_code, 200)
        trace_id = trace_response.json()["trace_id"]
        runtime_logs_response = client.get(f"/traces/{trace_id}/logs")
        self.assertEqual(runtime_logs_response.status_code, 200)
        runtime_rows = runtime_logs_response.json()
        self.assertTrue(runtime_rows)
        self.assertTrue(all(row["trace_id"] == trace_id for row in runtime_rows))
        self.assertTrue(all(row["data"]["run_id"] == run_id for row in runtime_rows if row["event"] == "runtime_event"))
        self.assertTrue(all(row["data"]["task_id"] == task_id for row in runtime_rows if row["event"] == "runtime_event"))
        log_file = current_log_file(client.app.state.project_root_for_tests / "logs")
        with log_file.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "ts": "2026-07-14T00:00:00+00:00",
                        "level": "INFO",
                        "logger": "coding_agent.backend",
                        "trace_id": trace_id,
                        "caller": "",
                        "event": "manually_injected_log",
                        "msg": "manually injected trace log",
                        "data": {"run_id": run_id, "task_id": task_id, "token": "[REDACTED]"},
                        "error": None,
                        "truncated": False,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        logs_response = client.get(f"/traces/{trace_id}/logs", params={"level": "INFO"})

        self.assertEqual(logs_response.status_code, 200)
        rows = logs_response.json()
        self.assertTrue(rows)
        self.assertTrue(all(row["trace_id"] == trace_id for row in rows))
        self.assertTrue(all(row["level"] == "INFO" for row in rows))
        self.assertTrue(any(row["msg"] == "manually injected trace log" for row in rows))
        self.assertFalse(_sqlite_table_exists(client.app.state.project_root_for_tests / "app.sqlite3", "trace_logs"))

    def test_http_request_failure_logs_are_mutually_exclusive(self) -> None:
        """校验已处理 HTTP 失败和未捕获异常不会双写错误日志。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 404 或未捕获 500 的日志事件出现互相污染。

        副作用:
            创建进程内 FastAPI 客户端并写入临时日期日志文件。
        """

        from fastapi.testclient import TestClient

        client = self._build_client()

        @client.app.get("/boom")
        async def boom() -> dict:
            """触发未捕获异常的测试路由。

            参数:
                无。

            返回:
                永不返回，函数总是抛出异常。

            异常:
                RuntimeError: 始终抛出以触发顶层异常日志。

            副作用:
                无。
            """

            raise RuntimeError("boom")

        handled_trace_id = "11111111111111111111111111111111"
        unhandled_trace_id = "22222222222222222222222222222222"
        handled_response = client.get("/missing", headers={"x-trace-id": handled_trace_id})
        boom_client = TestClient(client.app, raise_server_exceptions=False)
        unhandled_response = boom_client.get("/boom", headers={"x-trace-id": unhandled_trace_id})

        for handler in logging.getLogger("coding_agent.backend").handlers:
            handler.flush()
        rows = [
            json.loads(line)
            for line in current_log_file(client.app.state.project_root_for_tests / "logs").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        handled_messages = _messages_for_trace(rows, handled_trace_id)
        unhandled_messages = _messages_for_trace(rows, unhandled_trace_id)

        self.assertEqual(handled_response.status_code, 404)
        self.assertEqual(unhandled_response.status_code, 500)
        self.assertIn("http_request_failed", handled_messages)
        self.assertNotIn("unhandled_exception", handled_messages)
        self.assertIn("http_unhandled_exception", unhandled_messages)
        self.assertNotIn("http_request_failed", unhandled_messages)
        self.assertTrue(all(row.get("trace_id") in {handled_trace_id, unhandled_trace_id} for row in rows if row.get("trace_id") in {handled_trace_id, unhandled_trace_id}))

    def test_http_request_logging_accepts_x_trace_id(self) -> None:
        """校验请求日志接收客户端传入的 x-trace-id。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果响应 header 或 JSONL 请求日志没有携带传入的 trace 字段。

        副作用:
            创建进程内 FastAPI 客户端并写入临时日期日志文件。
        """

        client = self._build_client()
        trace_id = "1234567890abcdef1234567890abcdef"

        response = client.get(
            "/health",
            headers={
                "x-trace-id": trace_id,
            },
        )

        for handler in logging.getLogger("coding_agent.backend").handlers:
            handler.flush()
        rows = [
            json.loads(line)
            for line in current_log_file(client.app.state.project_root_for_tests / "logs").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        request_rows = [row for row in rows if row.get("trace_id") == trace_id]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-trace-id"], trace_id)
        self.assertTrue(request_rows)
        self.assertTrue(all(row["trace_id"] == trace_id for row in request_rows))

    def test_http_request_logging_rejects_invalid_x_trace_id(self) -> None:
        """校验非法 x-trace-id 不会污染后端请求 trace。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果后端沿用了非法 x-trace-id。

        副作用:
            创建进程内 FastAPI 客户端并写入临时日期日志文件。
        """

        client = self._build_client()
        cases = [
            "00000000000000000000000000000000",
            "not-a-trace",
            "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
            "1234567890abcdef",
        ]

        for trace_id in cases:
            with self.subTest(trace_id=trace_id):
                response = client.get(
                    "/health",
                    headers={
                        "x-trace-id": trace_id,
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertRegex(response.headers["x-trace-id"], r"^[0-9a-f]{32}$")
                self.assertNotEqual(response.headers["x-trace-id"], "00000000000000000000000000000000")
                self.assertNotEqual(response.headers["x-trace-id"], trace_id)

    def test_trace_logs_api_uses_local_date_for_offset_file_selection(self) -> None:
        """校验 trace logs API 按本地日期选择日期日志文件。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 UTC 查询时间没有命中本地日期日志文件。

        副作用:
            临时切换进程时区，创建测试客户端并写入日期日志文件。
        """

        with _temporary_timezone("Asia/Shanghai"):
            client = self._build_client()
            log_dir = client.app.state.project_root_for_tests / "logs"
            log_dir.mkdir(exist_ok=True)
            local_file = dated_log_file(log_dir, date(2026, 7, 15))
            local_file.write_text(
                json.dumps(
                    {
                        "ts": "2026-07-15T00:30:00+08:00",
                        "level": "INFO",
                        "logger": "coding_agent.backend",
                        "trace_id": "trace-api-local-date",
                        "caller": "",
                        "event": "api_local_date_selected",
                        "msg": "api local date selected",
                        "data": {"run_id": "run-api-local-date"},
                        "error": None,
                        "truncated": False,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            response = client.get(
                "/traces/trace-api-local-date/logs",
                params={
                    "start_time": "2026-07-14T16:00:00+00:00",
                    "end_time": "2026-07-14T17:00:00+00:00",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["msg"] for row in response.json()], ["api local date selected"])

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
        replay_response = client.get(f"/runs/{run.run_id}/replay")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(replay_response.status_code, 200)
        self.assertEqual(run_store.get_resume_command_by_key("resume:crash-window-key").status, "applied")
        self.assertEqual(runtime.get_task(task.task_id).status, "completed")
        self.assertEqual(run_store.get(run.run_id).status, "completed")
        resume_nodes = [node for node in replay_response.json()["nodes"] if node["node_type"] == "resume"]
        self.assertTrue(any(node["status"] == "completed" for node in resume_nodes))
        resume_details = [client.get(f"/replay/nodes/{node['replay_node_id']}").json() for node in resume_nodes]
        self.assertTrue(any(detail["payload"].get("status") == "resuming" for detail in resume_details))

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

        runtime = self._build_runtime()
        app = create_app(runtime=runtime)
        app.state.runtime_for_tests = runtime
        app.state.project_root_for_tests = runtime._project_root_for_tests
        return TestClient(app)

    def _build_tool_runtime_client(self, model_adapter=None, extra_tools=()):
        """为真实 ToolRuntime replay 测试构建 TestClient。

        参数:
            model_adapter: 可选模型适配器。
            extra_tools: 需要注册到测试 ToolRuntime 的额外工具定义。

        返回:
            接入 ToolRuntime、Trace、Replay 的 TestClient。

        异常:
            RuntimeError: 如果 FastAPI 依赖不可用。

        副作用:
            初始化临时项目根目录、SQLite 数据库和 JSONL 日志文件。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        runtime = self._build_runtime(
            model_adapter=model_adapter or ReplayToolModel(),
            with_tool_runtime=True,
            extra_tools=extra_tools,
        )
        app = create_app(runtime=runtime)
        app.state.runtime_for_tests = runtime
        app.state.project_root_for_tests = runtime._project_root_for_tests
        return TestClient(app)

    def _build_runtime(
        self,
        model_adapter=None,
        with_tool_runtime: bool = False,
        extra_tools=(),
    ) -> AgentRuntime:
        """为 API 测试构建一个隔离的运行时。

        参数:
            model_adapter: 可选模型适配器覆盖。
            with_tool_runtime: 是否接入 ToolRuntime 工具事实链路。
            extra_tools: 接入 ToolRuntime 时额外注册的工具定义。

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
        from app.config.logging import configure_logging

        log_dir = project_root / "logs"
        logger = configure_logging(log_dir)
        safe_tools = SafeReadTools(project_root)
        registry = ToolRegistry([*safe_tools.definitions(), *extra_tools])
        trace_store = TraceStore(project_root / "app.sqlite3")
        trace_recorder = TraceRecorder(trace_store, logger)
        trace_query_service = TraceQueryService(trace_store, log_dir)
        replay_service = ReplayService(trace_store)
        tool_runtime = _build_test_tool_runtime(
            project_root=project_root,
            database=project_root / "app.sqlite3",
            registry=registry,
            logger=logger,
        ) if with_tool_runtime else None
        runtime = AgentRuntime(
            settings=BackendSettings(
                project_root=project_root,
                log_dir=log_dir,
                database_file=project_root / "app.sqlite3",
            ),
            task_store=SQLiteTaskStore(project_root / "app.sqlite3"),
            context_builder=TextContextBuilder(),
            model_adapter=model_adapter or EchoStreamingModelAdapter(),
            tool_scheduler=ToolScheduler(
                registry=registry,
                allowed_permissions=("safe_read",),
                logger=logger,
            ),
            logger=logger,
            run_store=DurableRunStore(project_root / "app.sqlite3", logger),
            trace_recorder=trace_recorder,
            trace_query_service=trace_query_service,
            replay_service=replay_service,
            tool_runtime=tool_runtime,
        )
        runtime._project_root_for_tests = project_root
        return runtime

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
        trace_store = TraceStore(database)
        trace_recorder = TraceRecorder(trace_store, logger)
        replay_service = ReplayService(trace_store)
        resume_dispatcher = ResumeDispatcher(run_store, logger, trace_recorder=trace_recorder)
        approval_service = ApprovalService(
            approval_store=ApprovalStore(database),
            run_store=run_store,
            resume_dispatcher=resume_dispatcher,
            logger=logger,
            trace_recorder=trace_recorder,
        )
        runtime = AgentRuntime(
            settings=BackendSettings(
                project_root=project_root,
                log_dir=project_root / "logs",
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
            recovery_manager=RecoveryManager(run_store, logger, trace_recorder=trace_recorder),
            resume_dispatcher=resume_dispatcher,
            trace_recorder=trace_recorder,
            replay_service=replay_service,
        )
        return runtime, run_store, approval_service


if __name__ == "__main__":
    unittest.main()


def _sqlite_table_exists(database: Path, table_name: str) -> bool:
    """判断 SQLite 数据库中是否存在指定表。

    参数:
        database: SQLite 数据库路径。
        table_name: 表名。

    返回:
        表存在时返回 True。

    异常:
        sqlite3.Error: 如果数据库无法查询。

    副作用:
        打开 SQLite 数据库连接。
    """

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    return row is not None


def _messages_for_trace(rows: list[dict], trace_id: str) -> list[str]:
    """按 trace ID 提取日志消息。

    参数:
        rows: 已解析的 JSONL 日志行。
        trace_id: 前端用户操作 trace ID。

    返回:
        匹配请求 ID 的 event 列表。

    异常:
        无。

    副作用:
        无。
    """

    return [row["event"] for row in rows if row.get("trace_id") == trace_id]


@contextmanager
def _temporary_timezone(timezone_name: str):
    """临时切换进程本地时区。

    参数:
        timezone_name: IANA 时区名。

    生成:
        切换后的执行上下文。

    异常:
        unittest.SkipTest: 如果当前平台不支持 ``time.tzset``。

    副作用:
        修改并恢复当前进程的 ``TZ`` 环境变量。
    """

    if not hasattr(time, "tzset"):
        raise unittest.SkipTest("time.tzset is required for timezone-sensitive log file tests")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = timezone_name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _sqlite_tool_call_ids(database: Path) -> list[str]:
    """返回测试数据库中的工具调用标识列表。

    参数:
        database: SQLite 数据库路径。

    返回:
        按创建顺序排列的 tool_call_id 列表。

    异常:
        sqlite3.Error: 如果数据库无法查询。

    副作用:
        打开 SQLite 数据库连接。
    """

    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT tool_call_id FROM tool_calls ORDER BY created_at ASC").fetchall()
    return [row[0] for row in rows]


def _build_test_tool_runtime(project_root: Path, database: Path, registry: ToolRegistry, logger: logging.Logger):
    """构建测试用 ToolRuntime。

    参数:
        project_root: 临时项目根目录。
        database: SQLite 数据库路径。
        registry: 已包含 safe_read 工具的注册表。
        logger: 测试日志器。

    返回:
        配置了执行事实服务的 ToolRuntime。

    异常:
        无。

    副作用:
        初始化工具执行表和 artifact 目录。
    """

    from app.domain.artifacts.files import ArtifactFileStore
    from app.domain.artifacts.service import ArtifactService
    from app.domain.artifacts.store import ArtifactStore
    from app.tools.runtime.concurrency import ToolConcurrentScheduler
    from app.tools.execution.policy import ToolExecutionPolicy
    from app.tools.execution.policy_provider import PermissionPolicyProvider
    from app.tools.execution.service import ToolExecutionService
    from app.tools.execution.store import ToolExecutionStore
    from app.tools.executor import ToolCallExecutor
    from app.tools.runtime.locks import ToolResourceLockManager
    from app.tools.results import ToolObservationBuilder
    from app.tools.runtime.platform import ToolRuntime

    observation_builder = ToolObservationBuilder()
    execution_service = ToolExecutionService(
        store=ToolExecutionStore(database),
        policy=ToolExecutionPolicy(
            (
                PermissionPolicyProvider(
                    auto_approved_permissions=("safe_read",),
                    approval_required_permissions=("write_file", "command", "git_write"),
                ),
            )
        ),
        logger=logger,
    )
    artifact_service = ArtifactService(
        store=ArtifactStore(database),
        files=ArtifactFileStore(project_root / "storage" / "artifacts"),
    )
    return ToolRuntime(
        registry=registry,
        executor=ToolCallExecutor(
            observation_builder=observation_builder,
            logger=logger,
            artifact_service=artifact_service,
            execution_service=execution_service,
        ),
        observation_builder=observation_builder,
        logger=logger,
        execution_service=execution_service,
        concurrent_scheduler=ToolConcurrentScheduler(ToolResourceLockManager(), logger),
    )


class ReplayToolModel:
    """先请求 read_file 工具，再根据工具观测输出最终文本。"""

    async def stream(
        self,
        messages: list[RuntimeMessage],
        tools: list[ModelToolDefinition] = None,
    ):
        """产出用于真实 ToolRuntime replay 测试的模型增量。

        参数:
            messages: 当前运行时消息。
            tools: 本轮对模型可见的工具定义。

        生成:
            第一次调用请求 read_file，第二次调用输出最终文本。

        异常:
            无。

        副作用:
            无。
        """

        if not any(message.role == "tool" for message in messages):
            yield ModelDelta(
                text="",
                tool_call=ToolCall("read_file", {"path": "note.txt"}, "provider-call-1"),
            )
            return
        yield ModelDelta(text="tool done", is_final=True)


class MultiReplayToolModel:
    """先请求两个 read_file 工具，再根据工具观测输出最终文本。"""

    async def stream(
        self,
        messages: list[RuntimeMessage],
        tools: list[ModelToolDefinition] = None,
    ):
        """产出用于多工具 replay 测试的模型增量。

        参数:
            messages: 当前运行时消息。
            tools: 本轮对模型可见的工具定义。

        生成:
            第一次调用连续请求两个 read_file，后续调用输出最终文本。

        异常:
            无。

        副作用:
            无。
        """

        if not any(message.role == "tool" for message in messages):
            yield ModelDelta(
                text="",
                tool_call=ToolCall("read_file", {"path": "note-a.txt"}, "provider-call-a"),
            )
            yield ModelDelta(
                text="",
                tool_call=ToolCall("read_file", {"path": "note-b.txt"}, "provider-call-b"),
            )
            yield ModelDelta(text="", is_final=True)
            return
        yield ModelDelta(text="two tools done", is_final=True)


class ArtifactReplayToolModel:
    """先请求 large_output 工具，再根据工具观测输出最终文本。"""

    async def stream(
        self,
        messages: list[RuntimeMessage],
        tools: list[ModelToolDefinition] = None,
    ):
        """产出用于真实 artifact replay 测试的模型增量。

        参数:
            messages: 当前运行时消息。
            tools: 本轮对模型可见的工具定义。

        生成:
            第一次调用请求 large_output，后续调用输出最终文本。

        异常:
            无。

        副作用:
            无。
        """

        if not any(message.role == "tool" for message in messages):
            yield ModelDelta(
                text="",
                tool_call=ToolCall("large_output", {}, "provider-artifact-call"),
            )
            yield ModelDelta(text="", is_final=True)
            return
        yield ModelDelta(text="artifact done", is_final=True)


def _artifact_handler() -> ArtifactRequest:
    """返回测试用 artifact 创建请求。

    参数:
        无。

    返回:
        ArtifactRequest，交由 ToolCallExecutor 统一落盘。

    异常:
        无。

    副作用:
        无。实际文件写入由 artifact 服务完成。
    """

    return ArtifactRequest("command_output", "full command output", "captured command output")

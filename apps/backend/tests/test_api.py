"""閽堝浠诲姟鍒涘缓涓?SSE 娴佸紡浼犺緭鐨?FastAPI API 娴嬭瘯銆?"""

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

from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.core.trace.query_service import TraceQueryService
from app.core.trace.recorder import TraceRecorder
from app.config.logging import current_log_file, dated_log_file
from app.models.echo import EchoStreamingModelAdapter
from app.storage.crud.durable import DurableRunStore
from app.core.runtime.runner import AgentRuntime
from app.storage.crud.task import SQLiteTaskStore
from app.storage.crud.trace import TraceStore
from app.tools.registry.tool_registry import ToolRegistry
from app.tools.tool_handler.safe_read import SafeReadTools
from app.tools.runtime.compatibility import ToolScheduler


FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class BackendApiTests(unittest.TestCase):
    """鍦?FastAPI 渚濊禆鍙敤鏃舵牎楠?API 璺敱銆?"""

    def setUp(self) -> None:
        """鍒濆鍖?API 娴嬭瘯浣跨敤鐨勪复鏃剁洰褰曘€?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            鍒涘缓涓€涓敤浜庢寔鏈変复鏃剁洰褰曞彞鏌勭殑鍒楄〃锛屼娇鍏朵繚鎸佸瓨娲汇€?
        """

        self._temp_dirs = []
        self._runtimes = []
        self._clients = []

    def tearDown(self) -> None:
        """娓呯悊 API 娴嬭瘯浣跨敤鐨勪复鏃剁洰褰曘€?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            鍒犻櫎娴嬭瘯鍒涘缓鐨勪复鏃剁洰褰曘€?
        """

        for client in self._clients:
            client.close()
        for runtime in self._runtimes:
            runtime.close()
        for temp_dir in self._temp_dirs:
            temp_dir.cleanup()

    def _stream_task_latest_turn(self, client, task_id: str):
        """閫氳繃鏈€鏂?turn 鐨?SSE 绔偣杩愯鎴栧洖鏀句换鍔°€?

        鍙傛暟:
            client: FastAPI 娴嬭瘯瀹㈡埛绔€?
            task_id: 寰呰鍙栨渶鏂拌疆娆＄殑浠诲姟鏍囪瘑銆?

        杩斿洖:
            `/turns/{turn_id}/stream` 鐨勬祴璇曞搷搴斻€?

        寮傚父:
            AssertionError: 濡傛灉浠诲姟娌℃湁浠讳綍 turn銆?

        鍓綔鐢?
            鍙兘瑙﹀彂鏈€鏂?pending turn 鐨勮繍琛屻€?
        """

        turns_response = client.get(f"/tasks/{task_id}/turns")
        self.assertEqual(turns_response.status_code, 200)
        turns = turns_response.json()
        self.assertTrue(turns)
        return client.get(f"/turns/{turns[-1]['turn_id']}/stream")

    def _create_workspace_task(self, client, text: str):
        """閫氳繃 workspace task 涓诲崗璁垱寤轰换鍔°€?

        鍙傛暟:
            client: FastAPI 娴嬭瘯瀹㈡埛绔€?
            text: 浠诲姟杈撳叆鏂囨湰銆?

        杩斿洖:
            鍒涘缓浠诲姟鍝嶅簲銆?

        寮傚父:
            AssertionError: 濡傛灉宸ヤ綔鍖哄垱寤哄け璐ャ€?

        鍓綔鐢?
            鍒涘缓涓€涓复鏃舵祴璇曞伐浣滃尯涓庝换鍔°€?
        """

        workspace_response = client.post(
            "/workspaces",
            json={"name": f"workspace-{time.time_ns()}", "root_path": "/tmp/coding-agent-test"},
        )
        self.assertEqual(workspace_response.status_code, 200)
        workspace_id = workspace_response.json()["workspace_id"]
        return client.post(f"/workspaces/{workspace_id}/tasks", json={"text": text, "workspace_id": workspace_id})

    def test_task_creation_rejects_null_text(self) -> None:
        """鏍￠獙 API 鏍￠獙浼氭嫆缁濈┖浠诲姟鏂囨湰銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉鎺ュ彈浜嗙┖鏂囨湰銆?

        鍓綔鐢?
            鍒涘缓涓€涓繘绋嬪唴鐨?FastAPI 娴嬭瘯瀹㈡埛绔€?
        """

        client = self._build_client()
        workspace_response = client.post("/workspaces", json={"name": "validation", "root_path": "/tmp/validation"})
        self.assertEqual(workspace_response.status_code, 200)
        response = client.post(f"/workspaces/{workspace_response.json()['workspace_id']}/tasks", json={"text": None, "workspace_id": workspace_response.json()["workspace_id"]})

        self.assertEqual(response.status_code, 422)

    def test_health_reports_backend_model_configuration(self) -> None:
        """鏍￠獙鍋ュ悍妫€鏌ョ鐐逛細鏆撮湶褰撳墠妯″瀷閰嶇疆鎽樿銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉鍋ュ悍鐘舵€佺己灏?provider 鎴?Key 閰嶇疆鎽樿銆?

        鍓綔鐢?
            鍒涘缓涓€涓繘绋嬪唴鐨?FastAPI 娴嬭瘯瀹㈡埛绔€?
        """

        client = self._build_client()
        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["model_provider"], "echo")
        self.assertFalse(payload["has_model_api_key"])

    def test_task_stream_emits_sse_and_does_not_rerun_completed_task(self) -> None:
        """鏍￠獙浠诲姟娴佷細鍙戝嚭 SSE锛屼笖閲嶅璇诲彇娴佷細閲嶆斁浜嬩欢銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉娴佸紡浼犺緭澶辫触锛屾垨閲嶅璇诲彇杩藉姞浜嗕簨浠躲€?

        鍓綔鐢?
            鍦ㄥ簲鐢ㄧ殑娴嬭瘯鏁版嵁搴撲腑鍒涘缓浠诲姟鐘舵€併€?
        """

        client = self._build_client()
        create_response = self._create_workspace_task(client, "hello api")
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]
        self.assertEqual(create_response.json()["agent_id"], "developer")

        first_stream = self._stream_task_latest_turn(client, task_id)
        second_stream = self._stream_task_latest_turn(client, task_id)
        events_response = client.get(f"/tasks/{task_id}/events")

        self.assertEqual(first_stream.status_code, 200)
        self.assertIn("event: run_started", first_stream.text)
        self.assertIn("event: run_finished", first_stream.text)
        self.assertEqual(second_stream.status_code, 200)
        self.assertEqual(events_response.status_code, 200)
        self.assertEqual(len(events_response.json()), first_stream.text.count("event: "))

    def test_workspace_task_turn_stream_contract(self) -> None:
        """鏍￠獙 workspace/task/turn 鏂颁富鍗忚鍙互鍒涘缓銆佽繍琛屽拰鍥炴斁銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉鏂板崗璁鐐规病鏈夊舰鎴愬彲杩愯闂幆銆?

        鍓綔鐢?
            鍒涘缓宸ヤ綔鍖恒€佷换鍔°€佽拷鍔犺疆娆″苟杩愯 SSE銆?
        """

        client = self._build_client()
        workspace_response = client.post(
            "/workspaces",
            json={"name": "coding-agent", "root_path": "/tmp/coding-agent"},
        )
        self.assertEqual(workspace_response.status_code, 200)
        workspace_id = workspace_response.json()["workspace_id"]

        task_response = client.post(
            f"/workspaces/{workspace_id}/tasks",
            json={"text": "first turn", "workspace_id": workspace_id},
        )
        self.assertEqual(task_response.status_code, 200)
        task_payload = task_response.json()
        self.assertEqual(task_payload["workspace_id"], workspace_id)
        task_id = task_payload["task_id"]
        first_turn_id = task_payload["latest_turn_id"]

        first_stream = client.get(f"/turns/{first_turn_id}/stream")
        self.assertEqual(first_stream.status_code, 200)
        self.assertIn("event: run_started", first_stream.text)
        self.assertIn(f'"turn_id": "{first_turn_id}"', first_stream.text)

        turn_response = client.post(
            f"/tasks/{task_id}/turns",
            json={"input_text": "second turn"},
        )
        self.assertEqual(turn_response.status_code, 200)
        second_turn_id = turn_response.json()["turn_id"]
        turns_response = client.get(f"/tasks/{task_id}/turns")
        second_stream = client.get(f"/turns/{second_turn_id}/stream")
        replay_response = client.get(f"/turns/{second_turn_id}/stream")
        events_response = client.get(f"/tasks/{task_id}/events")

        self.assertEqual(turns_response.status_code, 200)
        self.assertEqual(len(turns_response.json()), 2)
        self.assertEqual(second_stream.status_code, 200)
        self.assertEqual(replay_response.status_code, 200)
        self.assertEqual(second_stream.text, replay_response.text)
        events = events_response.json()
        self.assertTrue(all("sequence" in event for event in events))
        self.assertEqual([event["sequence"] for event in events], sorted(event["sequence"] for event in events))

    def test_run_trace_endpoint_reads_trace_events_and_jsonl_logs(self) -> None:
        """鏍￠獙 run trace API 杩斿洖 ledger 浜嬩欢骞惰В鏋?JSONL 鏂囦欢鏃ュ織銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉 trace API 鏈繑鍥炰簨浠舵垨鏃ュ織銆?

        鍓綔鐢?
            鍒涘缓浠诲姟銆佽繍琛?SSE锛屽苟鍐欏叆涓存椂 JSONL 鏃ュ織銆?
        """

        client = self._build_client()
        create_response = self._create_workspace_task(client, "trace api")
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]

        stream_response = self._stream_task_latest_turn(client, task_id)
        self.assertEqual(stream_response.status_code, 200)
        run_id = client.app.state.runtime_for_tests._run_store.list_by_task(task_id)[-1].run_id

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

    def test_trace_logs_endpoint_reads_jsonl_without_trace_log_table(self) -> None:
        """鏍￠獙 trace logs API 鐩存帴璇诲彇 JSONL锛屽苟鏀寔 level 杩囨护銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉 API 渚濊禆 trace_logs 琛ㄦ垨鏈墽琛?level 杩囨护銆?

        鍓綔鐢?
            鍒涘缓浠诲姟銆佽繍琛?SSE锛屽苟閫氳繃 API 璇诲彇 JSONL 鏃ュ織銆?
        """

        client = self._build_client()
        create_response = self._create_workspace_task(client, "trace logs api")
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]

        stream_response = self._stream_task_latest_turn(client, task_id)
        self.assertEqual(stream_response.status_code, 200)
        run_id = client.app.state.runtime_for_tests._run_store.list_by_task(task_id)[-1].run_id
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
        """鏍￠獙宸插鐞?HTTP 澶辫触鍜屾湭鎹曡幏寮傚父涓嶄細鍙屽啓閿欒鏃ュ織銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉 404 鎴栨湭鎹曡幏 500 鐨勬棩蹇椾簨浠跺嚭鐜颁簰鐩告薄鏌撱€?

        鍓綔鐢?
            鍒涘缓杩涚▼鍐?FastAPI 瀹㈡埛绔苟鍐欏叆涓存椂鏃ユ湡鏃ュ織鏂囦欢銆?
        """

        from fastapi.testclient import TestClient

        client = self._build_client()

        @client.app.get("/boom")
        async def boom() -> dict:
            """瑙﹀彂鏈崟鑾峰紓甯哥殑娴嬭瘯璺敱銆?

            鍙傛暟:
                鏃犮€?

            杩斿洖:
                姘镐笉杩斿洖锛屽嚱鏁版€绘槸鎶涘嚭寮傚父銆?

            寮傚父:
                RuntimeError: 濮嬬粓鎶涘嚭浠ヨЕ鍙戦《灞傚紓甯告棩蹇椼€?

            鍓綔鐢?
                鏃犮€?
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
        """鏍￠獙璇锋眰鏃ュ織鎺ユ敹瀹㈡埛绔紶鍏ョ殑 x-trace-id銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉鍝嶅簲 header 鎴?JSONL 璇锋眰鏃ュ織娌℃湁鎼哄甫浼犲叆鐨?trace 瀛楁銆?

        鍓綔鐢?
            鍒涘缓杩涚▼鍐?FastAPI 瀹㈡埛绔苟鍐欏叆涓存椂鏃ユ湡鏃ュ織鏂囦欢銆?
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
        """鏍￠獙闈炴硶 x-trace-id 涓嶄細姹℃煋鍚庣璇锋眰 trace銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉鍚庣娌跨敤浜嗛潪娉?x-trace-id銆?

        鍓綔鐢?
            鍒涘缓杩涚▼鍐?FastAPI 瀹㈡埛绔苟鍐欏叆涓存椂鏃ユ湡鏃ュ織鏂囦欢銆?
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
        """鏍￠獙 trace logs API 鎸夋湰鍦版棩鏈熼€夋嫨鏃ユ湡鏃ュ織鏂囦欢銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            AssertionError: 濡傛灉 UTC 鏌ヨ鏃堕棿娌℃湁鍛戒腑鏈湴鏃ユ湡鏃ュ織鏂囦欢銆?

        鍓綔鐢?
            涓存椂鍒囨崲杩涚▼鏃跺尯锛屽垱寤烘祴璇曞鎴风骞跺啓鍏ユ棩鏈熸棩蹇楁枃浠躲€?
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

    def _build_client(self):
        """涓哄悗绔簲鐢ㄦ瀯寤?FastAPI TestClient銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            鍚庣 API 鐨?TestClient 瀹炰緥銆?

        寮傚父:
            RuntimeError: 濡傛灉 FastAPI 渚濊禆涓嶅彲鐢ㄣ€?

        鍓綔鐢?
            鍒濆鍖栧簲鐢ㄨ繍琛屾椂渚濊禆椤广€?
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        runtime = self._build_runtime()
        app = create_app(runtime=runtime)
        app.state.runtime_for_tests = runtime
        app.state.project_root_for_tests = runtime._project_root_for_tests
        client = TestClient(app)
        self._clients.append(client)
        return client

    def _build_runtime(
        self,
        model_adapter=None,
    ) -> AgentRuntime:
        """涓?API 娴嬭瘯鏋勫缓涓€涓殧绂荤殑杩愯鏃躲€?

        鍙傛暟:
            model_adapter: 鍙€夋ā鍨嬮€傞厤鍣ㄨ鐩栥€?
        杩斿洖:
            甯︽湁涓存椂椤圭洰鏍圭洰褰曘€佹暟鎹簱涓庢棩蹇楄矾寰勭殑 AgentRuntime銆?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            鍒涘缓涓€涓复鏃剁洰褰曚笌鍐呭瓨涓殑鏃ュ織璁板綍鍣ㄥ紩鐢ㄣ€?
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
        registry = ToolRegistry(safe_tools.definitions())
        trace_store = TraceStore(project_root / "app.sqlite3")
        trace_recorder = TraceRecorder(trace_store, logger)
        trace_query_service = TraceQueryService(trace_store, log_dir)
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
            run_store=DurableRunStore(project_root / "app.sqlite3"),
            trace_recorder=trace_recorder,
            trace_query_service=trace_query_service,
        )
        runtime._project_root_for_tests = project_root
        self._runtimes.append(runtime)
        return runtime

if __name__ == "__main__":
    unittest.main()


def _sqlite_table_exists(database: Path, table_name: str) -> bool:
    """鍒ゆ柇 SQLite 鏁版嵁搴撲腑鏄惁瀛樺湪鎸囧畾琛ㄣ€?

    鍙傛暟:
        database: SQLite 鏁版嵁搴撹矾寰勩€?
        table_name: 琛ㄥ悕銆?

    杩斿洖:
        琛ㄥ瓨鍦ㄦ椂杩斿洖 True銆?

    寮傚父:
        sqlite3.Error: 濡傛灉鏁版嵁搴撴棤娉曟煡璇€?

    鍓綔鐢?
        鎵撳紑 SQLite 鏁版嵁搴撹繛鎺ャ€?
    """

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    finally:
        connection.close()
    return row is not None


def _messages_for_trace(rows: list[dict], trace_id: str) -> list[str]:
    """鎸?trace ID 鎻愬彇鏃ュ織娑堟伅銆?

    鍙傛暟:
        rows: 宸茶В鏋愮殑 JSONL 鏃ュ織琛屻€?
        trace_id: 鍓嶇鐢ㄦ埛鎿嶄綔 trace ID銆?

    杩斿洖:
        鍖归厤璇锋眰 ID 鐨?event 鍒楄〃銆?

    寮傚父:
        鏃犮€?

    鍓綔鐢?
        鏃犮€?
    """

    return [row["event"] for row in rows if row.get("trace_id") == trace_id]


@contextmanager
def _temporary_timezone(timezone_name: str):
    """涓存椂鍒囨崲杩涚▼鏈湴鏃跺尯銆?

    鍙傛暟:
        timezone_name: IANA 鏃跺尯鍚嶃€?

    鐢熸垚:
        鍒囨崲鍚庣殑鎵ц涓婁笅鏂囥€?

    寮傚父:
        unittest.SkipTest: 濡傛灉褰撳墠骞冲彴涓嶆敮鎸?``time.tzset``銆?

    鍓綔鐢?
        淇敼骞舵仮澶嶅綋鍓嶈繘绋嬬殑 ``TZ`` 鐜鍙橀噺銆?
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

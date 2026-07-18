"""Trace Backbone 基础能力测试。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest

from app.core.trace.context import TraceContext
from app.core.trace.event_names import canonical_event_name, runtime_trace_event_name
from app.core.trace.ids import is_span_id, is_trace_id, new_span_id, new_trace_id
from app.core.trace.query_service import TraceQueryService
from app.core.trace.recorder import TraceRecorder
from app.core.trace.redaction import redact_value
from app.config.logging import configure_logging
from app.config.logging import current_log_file, dated_log_file, list_log_files
from app.config.logging import query_log_file
from app.storage.crud.trace import TraceStore


class TraceBackboneTests(unittest.TestCase):
    """验证 Trace Backbone 的本地事实源与 JSONL 日志边界。"""

    def test_trace_ids_use_expected_shape(self) -> None:
        """校验 trace/span 标识形态。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果生成的 ID 不符合本地约定。

        副作用:
            读取系统随机源。
        """

        self.assertTrue(is_trace_id(new_trace_id()))
        self.assertTrue(is_span_id(new_span_id()))

    def test_tool_finished_alias_maps_to_completed(self) -> None:
        """校验 Durable Run finished 旧名会归一化到 completed。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果事件别名映射漂移。

        副作用:
            无。
        """

        self.assertEqual(canonical_event_name("tool_execution_finished"), "tool_execution_completed")
        self.assertEqual(canonical_event_name("tool_call_finished"), "tool_execution_completed")
        self.assertEqual(canonical_event_name("run_finished"), "run_completed")
        self.assertEqual(canonical_event_name("tool_call_requested"), "tool_call_created")
        self.assertEqual(runtime_trace_event_name("tool_call_finished", {"status": "error"}), "tool_execution_failed")
        self.assertEqual(canonical_event_name("recovery_reconciled"), "recovery_reconciled")

    def test_redaction_removes_secret_values_and_truncates_text(self) -> None:
        """校验 trace/log payload 会脱敏和截断。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果敏感字段泄漏或大文本未截断。

        副作用:
            无。
        """

        payload = redact_value({"api_key": "secret-value", "text": "x" * 20}, max_text_length=5)

        self.assertEqual(payload["api_key"], "[REDACTED]")
        self.assertEqual(payload["text"], "xxxxx...[TRUNCATED:20]")

    def test_trace_recorder_persists_events_without_log_table(self) -> None:
        """校验 TraceRecorder 只写 event/span 表且 sequence 单调递增。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果事件未持久化或日志表被创建。

        副作用:
            创建临时 SQLite 数据库。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "app.sqlite3"
            store = TraceStore(database)
            logger = logging.getLogger(f"trace-test-{temp_dir}")
            logger.handlers = [logging.NullHandler()]
            recorder = TraceRecorder(store, logger)
            context = TraceContext(trace_id=new_trace_id(), task_id="task-1", run_id="run-1")

            recorder.record_event(context, "run_started", {"token": "secret"}, source="test")
            recorder.record_event(context, "run_finished", {"status": "completed"}, source="test")

            events = store.list_events(run_id="run-1")
            self.assertEqual([event.sequence_no for event in events], [1, 2])
            self.assertEqual(events[1].event_type, "run_completed")
            self.assertEqual(store.next_sequence("run-1"), 3)
            self.assertEqual(store.next_sequence("run-2"), 1)
            self.assertFalse(_sqlite_table_exists(database, "trace_logs"))

    def test_concurrent_events_get_unique_sequence_numbers(self) -> None:
        """校验并发写入同一 run 时 sequence_no 不重复。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果并发写入产生重复或乱序 sequence_no。

        副作用:
            创建临时 SQLite 数据库并并发写入 trace event。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "app.sqlite3"
            store = TraceStore(database)
            logger = logging.getLogger(f"trace-concurrent-{temp_dir}")
            logger.handlers = [logging.NullHandler()]
            recorder = TraceRecorder(store, logger)
            context = TraceContext(trace_id=new_trace_id(), task_id="task-concurrent", run_id="run-concurrent")

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(
                    executor.map(
                        lambda index: recorder.record_event(
                            context,
                            "run_started",
                            {"index": index},
                            source="test",
                        ),
                        range(20),
                    )
                )

            sequences = [event.sequence_no for event in store.list_events(run_id="run-concurrent", limit=50)]
            self.assertEqual(sequences, list(range(1, 21)))

    def test_jsonl_logging_redacts_sensitive_attributes_and_supports_filters(self) -> None:
        """校验 JSONL 文件日志会脱敏，并支持 trace/run/level 过滤。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果日志文件未写入 JSON、敏感值泄漏或过滤失效。

        副作用:
            写入临时 JSONL 日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            log_file = current_log_file(log_dir)
            logger = configure_logging(log_dir)
            logger.info(
                "trace_filtered_message",
                extra={
                    "display_message": "trace filtered message",
                    "trace_id": "trace-a",
                    "run_id": "run-a",
                    "api_key": "secret-value",
                    "nested": {"token": "secret-token"},
                },
            )
            logger.info("other_message", extra={"trace_id": "trace-b"})
            logger.warning("warn_message", extra={"trace_id": "trace-a", "run_id": "run-a"})
            for handler in logger.handlers:
                handler.flush()

            content = log_file.read_text(encoding="utf-8")
            self.assertNotIn("secret-value", content)
            self.assertNotIn("secret-token", content)

            rows = query_log_file(log_file, trace_id="trace-a", level="info")

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["msg"], "trace filtered message")
            self.assertEqual(rows[0]["event"], "trace_filtered_message")
            self.assertEqual(rows[0]["trace_id"], "trace-a")
            self.assertEqual(rows[0]["data"]["run_id"], "run-a")
            self.assertEqual(rows[0]["level"], "INFO")
            self.assertEqual(rows[0]["data"]["api_key"], "[REDACTED]")
            self.assertEqual(rows[0]["data"]["nested"]["token"], "[REDACTED]")

            raw_rows = [json.loads(line) for line in content.splitlines() if line.strip()]
            self.assertEqual(len(raw_rows), 3)
            self.assertEqual(query_log_file(log_file, trace_id="trace-a", level="warning")[0]["msg"], "warn_message")

    def test_jsonl_logging_supports_trace_filter_for_user_operation(self) -> None:
        """校验 JSONL 日志支持按 trace_id 查询一次用户操作链路。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 trace 过滤条件失效。

        副作用:
            写入临时 JSONL 日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            log_file = current_log_file(log_dir)
            logger = configure_logging(log_dir)
            logger.info("client_operation_one", extra={"trace_id": "trace-1"})
            logger.info("client_operation_two", extra={"trace_id": "trace-2"})
            for handler in logger.handlers:
                handler.flush()

            rows = query_log_file(log_file, trace_id="trace-1")

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["msg"], "client_operation_one")
            self.assertEqual(rows[0]["trace_id"], "trace-1")

    def test_get_run_trace_queries_logs_by_trace_id(self) -> None:
        """校验 get_run_trace 按 trace_id（非 run_id）聚合日志（D4）。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果日志未按 trace_id 过滤。

        副作用:
            写入临时 JSONL 日志与 trace 事件。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_dir = root / "logs"
            log_file = current_log_file(log_dir)
            logger = configure_logging(log_dir)
            logger.info("run_x_log", extra={"trace_id": "trace-run-x", "run_id": "run-x"})
            logger.info("other_trace_log", extra={"trace_id": "trace-other"})
            for handler in logger.handlers:
                handler.flush()

            store = TraceStore(root / "app.sqlite3")
            recorder_logger = logging.getLogger("trace-recorder-test")
            recorder_logger.handlers = [logging.NullHandler()]
            recorder = TraceRecorder(store, recorder_logger)
            context = TraceContext(trace_id="trace-run-x", task_id="task-x", run_id="run-x")
            recorder.record_event(context, "run_started", {"status": "running"}, source="test")

            service = TraceQueryService(store, log_dir)
            result = service.get_run_trace("run-x")

            self.assertEqual(result["run_id"], "run-x")
            self.assertEqual(result["trace_id"], "trace-run-x")
            log_events = [row["event"] for row in result["logs"]]
            self.assertIn("run_x_log", log_events)
            self.assertNotIn("other_trace_log", log_events)

    def test_jsonl_logging_marks_truncated_and_filters_time_range(self) -> None:
        """校验 JSONL 日志会标记截断并支持时间范围过滤。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果截断标记或时间过滤失效。

        副作用:
            写入临时 JSONL 日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            log_file = current_log_file(log_dir)
            logger = configure_logging(log_dir)
            logger.info("x" * 3000, extra={"trace_id": "trace-truncated"})
            for handler in logger.handlers:
                handler.flush()

            rows = query_log_file(
                log_file,
                trace_id="trace-truncated",
                start_time="2000-01-01T00:00:00+00:00",
                end_time="2999-01-01T00:00:00+00:00",
            )

            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["truncated"])
            self.assertIn("[TRUNCATED:", rows[0]["msg"])
            self.assertEqual(query_log_file(log_file, trace_id="trace-truncated", end_time="2000-01-01T00:00:00+00:00"), [])

    def test_jsonl_time_filter_compares_real_instant_across_offsets(self) -> None:
        """校验日志时间过滤按真实时刻比较而不是按字符串比较。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果不同时区 offset 的等价时刻被错误过滤。

        副作用:
            写入临时 JSONL 日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "logs-2026-07-15.log"
            log_file.write_text(
                json.dumps(
                    {
                        "ts": "2026-07-15T09:00:00+08:00",
                        "level": "INFO",
                        "logger": "test",
                        "trace_id": "trace-offset",
                        "caller": "",
                        "event": "offset_instant",
                        "msg": "offset instant",
                        "data": {},
                        "error": None,
                        "truncated": False,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            rows = query_log_file(
                log_file,
                trace_id="trace-offset",
                start_time="2026-07-15T01:00:00+00:00",
                end_time="2026-07-15T01:00:00+00:00",
            )

            self.assertEqual([row["msg"] for row in rows], ["offset instant"])

    def test_configure_logging_replaces_previous_file_handler(self) -> None:
        """校验重复配置日志文件不会串写旧文件。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果第二次配置后日志仍写入第一个文件。

        副作用:
            写入两个临时日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_dir = root / "first"
            second_dir = root / "second"
            first = current_log_file(first_dir)
            second = current_log_file(second_dir)
            configure_logging(first_dir).info("first_message")
            logger = configure_logging(second_dir)
            logger.info("second_message")
            for handler in logger.handlers:
                handler.flush()

            self.assertIn("first_message", first.read_text(encoding="utf-8"))
            self.assertNotIn("second_message", first.read_text(encoding="utf-8"))
            self.assertIn("second_message", second.read_text(encoding="utf-8"))

    def test_log_file_resolver_lists_date_range(self) -> None:
        """校验日期日志 resolver 只返回存在的日期文件。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果日期文件命名或范围过滤不符合约定。

        副作用:
            在临时目录创建日期日志文件。
        """

        from datetime import date

        with _temporary_timezone("UTC"), tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            log_dir.mkdir()
            first = dated_log_file(log_dir, date(2026, 7, 14))
            second = dated_log_file(log_dir, date(2026, 7, 15))
            first.write_text("", encoding="utf-8")
            second.write_text("", encoding="utf-8")

            files = list_log_files(
                log_dir,
                start_time="2026-07-14T00:00:00+00:00",
                end_time="2026-07-16T00:00:00+00:00",
            )

            self.assertEqual(files, [first, second])

    def test_trace_query_service_reads_cross_date_log_files(self) -> None:
        """校验 TraceQueryService 能按时间范围读取多个日期日志文件。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果查询服务只读取单个日期日志文件。

        副作用:
            创建临时 SQLite 数据库和两个日期日志文件。
        """

        from datetime import date

        with _temporary_timezone("UTC"), tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_dir = root / "logs"
            log_dir.mkdir()
            first = dated_log_file(log_dir, date(2026, 7, 14))
            second = dated_log_file(log_dir, date(2026, 7, 15))
            first.write_text(_json_log_line("2026-07-14T23:30:00+00:00", "first day") + "\n", encoding="utf-8")
            second.write_text(_json_log_line("2026-07-15T09:30:00+08:00", "second day") + "\n", encoding="utf-8")
            service = TraceQueryService(TraceStore(root / "app.sqlite3"), log_dir)

            rows = service.list_logs(
                trace_id="trace-cross",
                start_time="2026-07-14T23:00:00+00:00",
                end_time="2026-07-15T02:00:00+00:00",
            )

            self.assertEqual([row["msg"] for row in rows], ["first day", "second day"])

    def test_trace_query_service_uses_local_date_for_offset_file_selection(self) -> None:
        """校验查询时间按本地日期选择日志文件。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 UTC 查询时间没有命中本地日期日志文件。

        副作用:
            临时切换进程时区并创建日期日志文件。
        """

        from datetime import date

        with _temporary_timezone("Asia/Shanghai"), tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_dir = root / "logs"
            log_dir.mkdir()
            local_file = dated_log_file(log_dir, date(2026, 7, 15))
            local_file.write_text(
                _json_log_line("2026-07-15T00:30:00+08:00", "local date selected") + "\n",
                encoding="utf-8",
            )
            service = TraceQueryService(TraceStore(root / "app.sqlite3"), log_dir)

            rows = service.list_logs(
                trace_id="trace-cross",
                start_time="2026-07-14T16:00:00+00:00",
                end_time="2026-07-14T17:00:00+00:00",
            )

            self.assertEqual([row["msg"] for row in rows], ["local date selected"])


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

    import sqlite3

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    return row is not None


def _json_log_line(ts: str, message: str) -> str:
    """构造测试用 JSONL 日志行（9 字段 schema）。

    参数:
        ts: ISO 时间文本。
        message: 日志消息（映射到 9 字段的 msg）。

    返回:
        JSON 字符串。

    异常:
        TypeError: 如果测试数据无法序列化。

    副作用:
        无。
    """

    return json.dumps(
        {
            "ts": ts,
            "level": "INFO",
            "logger": "test",
            "trace_id": "trace-cross",
            "caller": "",
            "event": "cross_date_log",
            "msg": message,
            "data": {},
            "error": None,
            "truncated": False,
        }
    )


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


if __name__ == "__main__":
    unittest.main()

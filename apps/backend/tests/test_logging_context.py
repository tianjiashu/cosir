"""日志上下文自动注入测试。"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.core.trace.context import TraceContext
from app.config.logging import configure_logging
from app.config.logging import LogContext, merge_log_context, reset_log_context, set_log_context
from app.config.logging import current_log_file


class LoggingContextTests(unittest.TestCase):
    """校验 LogContext 能自动注入结构化 JSONL 日志。"""

    def test_log_context_injects_trace_fields(self) -> None:
        """校验 trace/run 上下文会注入日志。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 JSONL 顶层字段缺少 trace 或 run 关联 ID。

        副作用:
            写入临时日期日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            logger = configure_logging(log_dir)
            trace_token = set_log_context(LogContext(trace_id="trace-parent"))
            try:
                run_token = set_log_context(TraceContext(trace_id="trace-1", task_id="task-1", run_id="run-1"))
                try:
                    logger.info(
                        "context_injected",
                        extra={"operation": "unit", "data": {"run_id": "run-1", "task_id": "task-1"}},
                    )
                finally:
                    reset_log_context(run_token)
            finally:
                reset_log_context(trace_token)
            _flush(logger)

            row = _read_rows(current_log_file(log_dir))[0]
            self.assertEqual(row["event"], "context_injected")
            self.assertEqual(row["trace_id"], "trace-1")
            self.assertEqual(row["data"]["run_id"], "run-1")
            self.assertEqual(row["data"]["task_id"], "task-1")
            self.assertEqual(row["data"]["operation"], "unit")

    def test_merge_log_context_adds_local_id_without_losing_trace(self) -> None:
        """校验局部上下文合并不会丢失 trace 上下文。

        tool_call_id 已不再是链路键（被 merge_log_context 忽略），只能落入 data。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 tool_call_id 未落入 data 或 trace_id 丢失。

        副作用:
            写入临时日期日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            logger = configure_logging(log_dir)
            trace_token = set_log_context(LogContext(trace_id="trace-1"))
            try:
                logger.info("tool_context_injected", extra={"data": {"tool_call_id": "tool-1"}})
            finally:
                reset_log_context(trace_token)
            _flush(logger)

            row = _read_rows(current_log_file(log_dir))[0]
            self.assertEqual(row["trace_id"], "trace-1")
            self.assertEqual(row["data"]["tool_call_id"], "tool-1")

    def test_explicit_extra_overrides_context_field(self) -> None:
        """校验 logging extra 显式字段优先于上下文默认值。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 filter 覆盖了调用方显式 extra 字段。

        副作用:
            写入临时日期日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            logger = configure_logging(log_dir)
            token = set_log_context(LogContext(trace_id="context-trace"))
            try:
                logger.info("explicit_extra_wins", extra={"trace_id": "explicit-trace"})
            finally:
                reset_log_context(token)
            _flush(logger)

            row = _read_rows(current_log_file(log_dir))[0]
            self.assertEqual(row["trace_id"], "explicit-trace")

    def test_exception_log_keeps_context_and_stack(self) -> None:
        """校验异常日志自动携带上下文和堆栈。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果异常日志缺少 trace_id 或 stack。

        副作用:
            写入临时日期日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            logger = configure_logging(log_dir)
            token = set_log_context(LogContext(trace_id="trace-error"))
            try:
                try:
                    raise RuntimeError("context failure")
                except RuntimeError:
                    logger.exception("context_exception")
            finally:
                reset_log_context(token)
            _flush(logger)

            row = _read_rows(current_log_file(log_dir))[0]
            self.assertEqual(row["trace_id"], "trace-error")
            self.assertEqual(row["error"]["type"], "RuntimeError")
            self.assertIn("Traceback", row["error"]["stack"])

    def test_log_context_does_not_cross_async_tasks(self) -> None:
        """校验并发异步任务中的日志上下文不会串线。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果两个异步任务写出的 trace_id 互相污染。

        副作用:
            写入临时日期日志文件。
        """

        async def write_with_context(logger, trace_id: str) -> None:
            """在独立异步任务中绑定上下文并写日志。

            参数:
                logger: Python logger。
                trace_id: 当前异步任务的 trace ID。

            返回:
                无。

            异常:
                无。

            副作用:
                写入一条结构化日志。
            """

            token = set_log_context(LogContext(trace_id=trace_id))
            try:
                await asyncio.sleep(0)
                logger.info("async_context_logged")
            finally:
                reset_log_context(token)

        async def write_both(logger) -> None:
            """并发写入两条不同请求上下文日志。

            参数:
                logger: Python logger。

            返回:
                无。

            异常:
                无。

            副作用:
                并发写入两条结构化日志。
            """

            await asyncio.gather(write_with_context(logger, "trace-a"), write_with_context(logger, "trace-b"))

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            logger = configure_logging(log_dir)
            asyncio.run(write_both(logger))
            _flush(logger)

            trace_ids = {row["trace_id"] for row in _read_rows(current_log_file(log_dir))}
            self.assertEqual(trace_ids, {"trace-a", "trace-b"})

    def test_lookup_after_trace_reset_reuses_only_durable_run_context(self) -> None:
        """校验 run/task 反查只继承已登记的 durable run 上下文。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 durable run 上下文无法补齐 trace/task/run。

        副作用:
            写入临时日期日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            logger = configure_logging(log_dir)
            run_token = set_log_context(TraceContext(trace_id="trace-1", task_id="task-1", run_id="run-1"))
            reset_log_context(run_token)
            merge_token = merge_log_context(run_id="run-1")
            try:
                logger.info("lookup_after_trace_reset", extra={"data": {"run_id": "run-1", "task_id": "task-1"}})
            finally:
                reset_log_context(merge_token)
            _flush(logger)

            row = _read_rows(current_log_file(log_dir))[0]
            self.assertEqual(row["trace_id"], "trace-1")
            self.assertEqual(row["data"]["task_id"], "task-1")
            self.assertEqual(row["data"]["run_id"], "run-1")

    def test_merge_log_context_with_run_id_backfills_trace_and_task(self) -> None:
        """校验只合并 run_id 时会自动补齐 trace_id 和 task_id。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果局部边界日志只包含 run_id。

        副作用:
            写入临时日期日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            logger = configure_logging(log_dir)
            bind_token = set_log_context(TraceContext(trace_id="trace-2", task_id="task-2", run_id="run-2"))
            reset_log_context(bind_token)
            run_token = merge_log_context(run_id="run-2")
            try:
                logger.info("run_context_backfilled", extra={"data": {"run_id": "run-2", "task_id": "task-2"}})
            finally:
                reset_log_context(run_token)
            _flush(logger)

            row = _read_rows(current_log_file(log_dir))[0]
            self.assertEqual(row["trace_id"], "trace-2")
            self.assertEqual(row["data"]["task_id"], "task-2")
            self.assertEqual(row["data"]["run_id"], "run-2")

    def test_message_text_does_not_backfill_durable_context(self) -> None:
        """校验日志消息文本中的 run_id 不会触发上下文反查。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果自由文本消息污染了结构化日志上下文。

        副作用:
            写入临时日期日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            logger = configure_logging(log_dir)
            bind_token = set_log_context(TraceContext(trace_id="trace-3", task_id="task-3", run_id="run-3"))
            reset_log_context(bind_token)
            logger.info("plain text mentions run_id=run-3 but has no structured extra")
            _flush(logger)

            row = _read_rows(current_log_file(log_dir))[0]
            self.assertEqual(row["trace_id"], "")
            self.assertNotIn("task_id", row)
            self.assertNotIn("run_id", row)


def _flush(logger) -> None:
    """刷新 logger 上挂载的 handler。

    参数:
        logger: Python logger。

    返回:
        无。

    异常:
        OSError: 如果 handler 刷新失败。

    副作用:
        将缓冲日志刷入磁盘。
    """

    for handler in logger.handlers:
        handler.flush()


def _read_rows(path: Path) -> list[dict]:
    """读取 JSONL 日志行为字典列表。

    参数:
        path: JSONL 日志文件路径。

    返回:
        已解析日志行列表。

    异常:
        json.JSONDecodeError: 如果日志行不是合法 JSON。
        OSError: 如果日志文件无法读取。

    副作用:
        读取日志文件。
    """

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()

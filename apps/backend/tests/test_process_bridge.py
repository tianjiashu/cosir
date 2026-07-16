"""跨进程日志桥接测试。"""

import tempfile
import time
import unittest
from pathlib import Path

from app.config.logging import configure_logging
from app.config.logging import current_log_file
from app.config.logging import get_log_queue, stop_queue_listener
from app.config.logging import SQLiteLogHandler
from app.storage.log_records import LogQuery
from app.storage.log_store import LogStore
from app.tools.execute import execute_tool_handler


def _failing_handler() -> str:
    """始终抛异常的测试 handler，用于验证子进程日志回传。

    参数:
        无。

    返回:
        永不返回。

    异常:
        RuntimeError: 始终抛出，模拟工具崩溃。

    副作用:
        无。
    """
    raise RuntimeError("subprocess boom")


class ProcessBridgeTests(unittest.TestCase):
    """验证 spawn 子进程日志能经队列回到父进程统一管线。"""

    def test_subprocess_tool_failure_logged_with_run_id(self) -> None:
        """校验子进程 handler 异常日志进入父进程 JSONL 且携带 run_id。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果日志未落盘或缺失 run_id / 堆栈。

        副作用:
            创建临时日志目录并启动一次 spawn 子进程。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            log_file = current_log_file(log_dir)
            logger = configure_logging(log_dir)
            queue = get_log_queue()
            self.assertIsNotNone(queue)

            result = execute_tool_handler(
                handler=_failing_handler,
                arguments={},
                timeout_seconds=5,
                log_queue=queue,
                run_id="run-bridge-1",
            )
            self.assertEqual(result.status, "error")

            self._wait_for_log_line(log_file, "tool_handler_failed", timeout=3.0)
            content = log_file.read_text(encoding="utf-8")
            self.assertIn("run-bridge-1", content)
            self.assertIn("RuntimeError", content)
            self.assertIn("subprocess boom", content)

    def _wait_for_log_line(self, log_file: Path, needle: str, timeout: float = 3.0) -> None:
        """轮询等待日志文件出现目标文本。

        参数:
            log_file: 待检查的 JSONL 日志文件。
            needle: 需要出现的子串。
            timeout: 最大等待秒数。

        返回:
            无。

        异常:
            AssertionError: 如果超时仍未出现目标文本。

        副作用:
            无。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if log_file.exists() and needle in log_file.read_text(encoding="utf-8"):
                return
            time.sleep(0.05)
        self.fail(f"log line '{needle}' not found in {log_file}")

    def test_subprocess_tool_failure_logged_to_sqlite_with_run_id(self) -> None:
        """校验子进程异常日志经队列回传后写入 SQLite 且关联 run_id。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 SQLite 未入库或 run_id/异常类型不匹配。

        副作用:
            创建临时日志目录与 SQLite 日志库，并启动一次 spawn 子进程。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_dir = root / "logs"
            db_file = root / "logs.sqlite3"
            logger = configure_logging(
                log_dir,
                log_database_file=db_file,
                sqlite_logging_enabled=True,
            )
            queue = get_log_queue()
            self.assertIsNotNone(queue)

            result = execute_tool_handler(
                handler=_failing_handler,
                arguments={},
                timeout_seconds=5,
                log_queue=queue,
                run_id="run-bridge-sqlite",
            )
            self.assertEqual(result.status, "error")

            for handler in logger.handlers:
                if isinstance(handler, SQLiteLogHandler):
                    handler.flush()

            entries = LogStore(db_file).query(LogQuery(run_id="run-bridge-sqlite"))
            events = [entry.event_name for entry in entries]
            self.assertIn("tool_handler_failed", events)
            failed = [entry for entry in entries if entry.event_name == "tool_handler_failed"][0]
            self.assertEqual(failed.run_id, "run-bridge-sqlite")
            self.assertEqual(failed.error_type, "RuntimeError")

    def tearDown(self) -> None:
        stop_queue_listener()


if __name__ == "__main__":
    unittest.main()

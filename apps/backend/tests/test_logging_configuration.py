"""针对后端文件日志配置的测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config.logging import configure_logging
from app.config.logging import current_log_file
from app.config.logging import SQLiteLogHandler


class LoggingConfigurationTests(unittest.TestCase):
    """校验后端日志会写入配置好的日志文件。"""

    def test_configure_logging_writes_dated_log_file(self) -> None:
        """校验配置好的日志记录器会把诊断消息写入磁盘。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果日志文件未被创建或不包含期望的消息。

        副作用:
            创建一个临时目录并写入一个临时日志文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            log_file = current_log_file(log_dir)
            logger = configure_logging(log_dir)
            logger.info("test_log_message")
            logger.debug("test_debug_message")
            for handler in logger.handlers:
                handler.flush()

            self.assertTrue(log_file.exists())
            log_content = log_file.read_text(encoding="utf-8")
            self.assertIn("test_log_message", log_content)
            self.assertIn("test_debug_message", log_content)

    def test_sqlite_logging_initialization_failure_keeps_file_logging(self) -> None:
        """校验 SQLite 日志初始化失败时后端降级到文件日志。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果文件日志不可用或 SQLite handler 仍被挂载。

        副作用:
            创建临时日志目录并写入一条 warning 日志。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            log_file = current_log_file(log_dir)
            with patch(
                "app.config.logging.configuration.LogStore",
                side_effect=OSError("database is locked"),
            ):
                logger = configure_logging(
                    log_dir,
                    log_database_file=Path(temp_dir) / "logs.sqlite3",
                    sqlite_logging_enabled=True,
                )
            logger.info("file_log_still_available")
            for handler in logger.handlers:
                handler.flush()

            self.assertTrue(log_file.exists())
            log_content = log_file.read_text(encoding="utf-8")
            self.assertIn("sqlite_log_handler_unavailable", log_content)
            self.assertIn("file_log_still_available", log_content)
            self.assertFalse(any(isinstance(handler, SQLiteLogHandler) for handler in logger.handlers))


if __name__ == "__main__":
    unittest.main()

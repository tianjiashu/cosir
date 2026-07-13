"""针对后端文件日志配置的测试。"""

import tempfile
import unittest
from pathlib import Path

from app.logging.configuration import configure_logging


class LoggingConfigurationTests(unittest.TestCase):
    """校验后端日志会写入配置好的日志文件。"""

    def test_configure_logging_writes_log_file(self) -> None:
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
            log_file = Path(temp_dir) / "logs" / "app.log"
            logger = configure_logging(log_file)
            logger.info("test_log_message")
            for handler in logger.handlers:
                handler.flush()

            self.assertTrue(log_file.exists())
            self.assertIn("test_log_message", log_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

"""JSONL 日志格式测试。"""

import json
import logging
import re
import sys
import unittest

from app.config.logging import JsonlFormatter
from app.config.logging import MAX_LOG_TEXT_LENGTH
from app.config.logging import entry_from_log_record


class JsonlFormatterTests(unittest.TestCase):
    """校验 JSONL 文件日志字段契约。"""

    def test_formatter_outputs_event_msg_data_and_nested_error(self) -> None:
        """校验异常日志输出 event、msg、data 与嵌套 error 块。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 JSONL 字段缺失。

        副作用:
            无。
        """

        formatter = JsonlFormatter()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            exc_info = sys.exc_info()
            record = logging.getLogger("coding_agent.backend").makeRecord(
                name="coding_agent.backend",
                level=logging.ERROR,
                fn=__file__,
                lno=1,
                msg="tool_call_failed",
                args=(),
                exc_info=exc_info,
                extra={"msg": "工具执行失败", "tool_name": "read_file"},
            )
        payload = json.loads(formatter.format(record))
        self.assertEqual(payload["event"], "tool_call_failed")
        self.assertEqual(payload["msg"], "工具执行失败")
        self.assertEqual(payload["data"]["tool_name"], "read_file")
        self.assertEqual(payload["error"]["type"], "RuntimeError")
        self.assertIn("Traceback", payload["error"]["stack"])

    def test_formatter_matches_sqlite_mapping_for_context_and_fallback_event(self) -> None:
        """校验 JSONL 与 SQLite 映射语义保持一致。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果两种落盘格式字段不一致。

        副作用:
            无。
        """

        formatter = JsonlFormatter()
        record = logging.getLogger("coding_agent.backend").makeRecord(
            name="coding_agent.backend",
            level=logging.DEBUG,
            fn=__file__,
            lno=1,
            msg="用户可读消息",
            args=(),
            exc_info=None,
            extra={
                "trace_id": "trace-1",
                "task_id": "task-1",
                "run_id": "run-1",
                "msg": "x" * (MAX_LOG_TEXT_LENGTH + 1),
            },
        )

        payload = json.loads(formatter.format(record))
        entry = entry_from_log_record(record)

        self.assertEqual(payload["event"], "log_event")
        self.assertEqual(entry.event, "log_event")
        self.assertEqual(payload["msg"], entry.msg)
        self.assertTrue(payload["truncated"])
        self.assertTrue(entry.truncated)
        self.assertEqual(payload["trace_id"], "trace-1")
        # 非保留链路键落入 data（Phase 1 兼容噪音，Phase 2 清理）。
        self.assertEqual(payload["data"]["task_id"], "task-1")
        self.assertEqual(payload["data"]["run_id"], "run-1")
        self.assertRegex(payload["ts"], re.compile(r"^\d{4}-\d{2}-\d{2}T.*\.\d{3}Z$"))

    def test_message_defaults_to_event_name_without_msg(self) -> None:
        """校验没有 msg 时消息文本使用原始事件名。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果默认 message 语义变化。

        副作用:
            无。
        """

        record = logging.getLogger("coding_agent.backend").makeRecord(
            name="coding_agent.backend",
            level=logging.INFO,
            fn=__file__,
            lno=1,
            msg="http_request_finished",
            args=(),
            exc_info=None,
        )
        payload = json.loads(JsonlFormatter().format(record))
        self.assertEqual(payload["event"], "http_request_finished")
        self.assertEqual(payload["msg"], "http_request_finished")

    def test_message_defaults_to_fallback_event_name_for_dynamic_text(self) -> None:
        """校验动态文本不会进入默认 message 字段。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果默认 message 仍包含动态变量。

        副作用:
            无。
        """

        record = logging.getLogger("coding_agent.backend").makeRecord(
            name="coding_agent.backend",
            level=logging.INFO,
            fn=__file__,
            lno=1,
            msg="Human readable %s",
            args=("value",),
            exc_info=None,
        )
        payload = json.loads(JsonlFormatter().format(record))
        self.assertEqual(payload["event"], "log_event")
        self.assertEqual(payload["msg"], "log_event")


if __name__ == "__main__":
    unittest.main()

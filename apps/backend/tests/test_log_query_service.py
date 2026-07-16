"""日志查询服务测试。"""

import unittest

from app.core.logs.query_service import LogQueryService
from app.storage.log_records import LogEntryRecord


class LogQueryServiceTests(unittest.TestCase):
    """校验日志查询服务参数规则。"""

    def test_query_by_trace_normalizes_level_time_and_order(self) -> None:
        """校验 trace 查询参数归一化。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果查询参数不符合预期。

        副作用:
            无。
        """

        store = _Store()
        service = LogQueryService(store, max_limit=10)
        result = service.query_by_trace(
            trace_id="trace-1",
            level="warn",
            start_time="2026-07-15T10:00:00Z",
            limit=5,
        )
        self.assertIn('event=test_event message="rendered"', result.text)
        self.assertEqual(store.last_query.trace_id, "trace-1")
        self.assertEqual(store.last_query.level, "WARNING")
        self.assertEqual(store.last_query.start_time, "2026-07-15T10:00:00.000Z")
        self.assertEqual(store.last_query.order, "asc")

    def test_recent_uses_desc_order_and_limit_validation(self) -> None:
        """校验 recent 查询倒序并限制 limit。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果排序或 limit 校验不符合预期。

        副作用:
            无。
        """

        store = _Store()
        service = LogQueryService(store, max_limit=2)
        service.recent(limit=2)
        self.assertEqual(store.last_query.order, "desc")
        with self.assertRaises(ValueError):
            service.recent(limit=3)

    def test_invalid_level_and_blank_trace_raise_value_error(self) -> None:
        """校验非法级别和空 trace_id 会抛出 ValueError。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果非法参数未被拒绝。

        副作用:
            无。
        """

        service = LogQueryService(_Store())
        with self.assertRaises(ValueError):
            service.query_by_trace(trace_id="")
        with self.assertRaises(ValueError):
            service.recent(level="NOTICE")


class _Store:
    """测试用日志存储。"""

    def __init__(self) -> None:
        """初始化测试存储。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self.last_query = None

    def query(self, query):
        """记录查询参数并返回一条测试日志。

        参数:
            query: 查询参数。

        返回:
            日志记录列表。

        异常:
            无。

        副作用:
            保存最近一次查询参数。
        """

        self.last_query = query
        return [
            LogEntryRecord(
                ts="2026-07-15T10:00:00.000Z",
                level="INFO",
                logger_name="coding_agent.backend",
                event_name="test_event",
                message="rendered",
            )
        ]


if __name__ == "__main__":
    unittest.main()

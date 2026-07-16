"""日志查询 API 测试。"""

import logging
import sqlite3
import unittest

from app.api.app import app as _app
from app.api.dependencies import set_runtime
from app.storage.log_records import LogEntryRecord, LogQueryResult

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class LogsApiTests(unittest.TestCase):
    """校验日志查询 API 契约。"""

    def test_query_logs_requires_trace_and_returns_text(self) -> None:
        """校验 /logs/query 返回结构化日志和纯文本。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果响应契约不符合预期。

        副作用:
            创建进程内 FastAPI 应用。
        """

        client = _client(_Runtime(_LogQueryService()))
        response = client.get("/logs/query?trace_id=trace-1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["entries"][0]["event"], "tool_call_failed")
        self.assertEqual(payload["text"], "rendered logs")

    def test_recent_logs_does_not_require_trace(self) -> None:
        """校验 /logs/recent 不要求 trace_id。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 recent 查询失败。

        副作用:
            创建进程内 FastAPI 应用。
        """

        client = _client(_Runtime(_LogQueryService()))
        response = client.get("/logs/recent?limit=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entries"][0]["event"], "tool_call_failed")

    def test_invalid_trace_query_returns_400(self) -> None:
        """校验非法 trace 查询返回 400。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果错误状态码不符合预期。

        副作用:
            创建进程内 FastAPI 应用。
        """

        client = _client(_Runtime(_LogQueryService()))
        response = client.get("/logs/query?trace_id=")
        self.assertEqual(response.status_code, 400)

    def test_invalid_level_and_time_return_400(self) -> None:
        """校验非法 level 和时间格式返回 400。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果参数校验状态码不符合预期。

        副作用:
            创建进程内 FastAPI 应用。
        """

        client = _client(_Runtime(_LogQueryService()))
        level_response = client.get("/logs/recent?level=NOTICE")
        time_response = client.get("/logs/query?trace_id=trace-1&start_time=bad-time")
        self.assertEqual(level_response.status_code, 400)
        self.assertEqual(time_response.status_code, 400)

    def test_limit_bounds_return_422(self) -> None:
        """校验 FastAPI limit 边界返回 422。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 limit 边界没有被 FastAPI 拒绝。

        副作用:
            创建进程内 FastAPI 应用。
        """

        client = _client(_Runtime(_LogQueryService()))
        low_response = client.get("/logs/recent?limit=0")
        high_response = client.get("/logs/query?trace_id=trace-1&limit=1001")
        self.assertEqual(low_response.status_code, 422)
        self.assertEqual(high_response.status_code, 422)

    def test_query_exception_returns_500_and_logs_error(self) -> None:
        """校验查询服务异常会返回 500。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果异常没有映射为 500。

        副作用:
            创建进程内 FastAPI 应用。
        """

        client = _client(_Runtime(_FailingLogQueryService()))
        response = client.get("/logs/recent")
        self.assertEqual(response.status_code, 500)

    def test_recent_empty_result_returns_empty_payload(self) -> None:
        """校验空结果返回空 entries 和空 text。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果空结果结构不稳定。

        副作用:
            创建进程内 FastAPI 应用。
        """

        client = _client(_Runtime(_EmptyLogQueryService()))
        response = client.get("/logs/recent")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"entries": [], "text": ""})

    def test_query_empty_result_returns_empty_payload(self) -> None:
        """校验 trace 查询空结果返回空 entries 和空 text。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果空结果结构不稳定。

        副作用:
            创建进程内 FastAPI 应用。
        """

        client = _client(_Runtime(_EmptyLogQueryService()))
        response = client.get("/logs/query?trace_id=trace-empty")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"entries": [], "text": ""})


class _Runtime:
    """测试用 Runtime 替身。"""

    def __init__(self, service) -> None:
        """保存日志查询服务。

        参数:
            service: 测试用日志查询服务。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self._service = service
        self._logger = logging.getLogger("coding_agent.backend")

    def close(self) -> None:
        """测试替身无需释放资源。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        return None

    def log_query_service(self):
        """返回日志查询服务。

        参数:
            无。

        返回:
            测试日志查询服务。

        异常:
            无。

        副作用:
            无。
        """

        return self._service

    def logger(self):
        """返回测试 logger。

        参数:
            无。

        返回:
            Python logger。

        异常:
            无。

        副作用:
            无。
        """

        return self._logger


class _LogQueryService:
    """测试用日志查询服务。"""

    def query_by_trace(self, trace_id: str, **kwargs):
        """返回按 trace 查询的测试结果。

        参数:
            trace_id: trace 标识。
            kwargs: API 传入的其他查询字段。

        返回:
            LogQueryResult。

        异常:
            ValueError: 如果 trace_id 为空。

        副作用:
            无。
        """

        if not trace_id:
            raise ValueError("trace_id must not be blank")
        _raise_for_invalid_kwargs(kwargs)
        return _result()

    def recent(self, **kwargs):
        """返回最近日志测试结果。

        参数:
            kwargs: API 传入的查询字段。

        返回:
            LogQueryResult。

        异常:
            无。

        副作用:
            无。
        """

        _raise_for_invalid_kwargs(kwargs)
        return _result()


class _FailingLogQueryService:
    """测试用失败日志查询服务。"""

    def recent(self, **kwargs):
        """模拟日志查询失败。

        参数:
            kwargs: API 查询参数。

        返回:
            永不返回。

        异常:
            sqlite3.OperationalError: 始终抛出。

        副作用:
            无。
        """
        raise sqlite3.OperationalError("query failed")


class _EmptyLogQueryService:
    """测试用空结果日志查询服务。"""

    def query_by_trace(self, trace_id: str, **kwargs):
        """返回空 trace 日志查询结果。

        参数:
            trace_id: trace 标识。
            kwargs: API 查询参数。

        返回:
            空 LogQueryResult。

        异常:
            ValueError: 如果 trace_id 为空。

        副作用:
            无。
        """

        if not trace_id:
            raise ValueError("trace_id must not be blank")
        return LogQueryResult(entries=[], text="")

    def recent(self, **kwargs):
        """返回空日志查询结果。

        参数:
            kwargs: API 查询参数。

        返回:
            空 LogQueryResult。

        异常:
            无。

        副作用:
            无。
        """

        return LogQueryResult(entries=[], text="")


def _raise_for_invalid_kwargs(kwargs) -> None:
    """按测试参数模拟查询服务的参数校验。

    参数:
        kwargs: API 传入的查询字段。

    返回:
        无。

    异常:
        ValueError: 当 level 或时间格式非法时抛出。

    副作用:
        无。
    """

    if kwargs.get("level") == "NOTICE":
        raise ValueError("invalid level")
    if kwargs.get("start_time") == "bad-time":
        raise ValueError("invalid start_time")


def _result() -> LogQueryResult:
    """构造测试日志查询结果。

    参数:
        无。

    返回:
        LogQueryResult。

    异常:
        无。

    副作用:
        无。
    """

    return LogQueryResult(
        entries=[
            LogEntryRecord(
                ts="2026-07-15T10:00:00.000Z",
                level="ERROR",
                logger="coding_agent.backend",
                trace_id="trace-1",
                caller="",
                event="tool_call_failed",
                msg="工具失败",
                error={"type": "RuntimeError", "message": "boom", "stack": "Traceback"},
            )
        ],
        text="rendered logs",
    )


def _client(runtime: _Runtime):
    """构建测试客户端。

    参数:
        runtime: 测试 Runtime 替身。

    返回:
        FastAPI TestClient。

    异常:
        RuntimeError: 如果 FastAPI 依赖不可用。

    副作用:
        创建进程内 FastAPI 应用。
    """

    set_runtime(runtime)
    return TestClient(_app)


if __name__ == "__main__":
    unittest.main()

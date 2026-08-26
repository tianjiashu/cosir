from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionTestResult:
    """一次连通性测试的结果值对象。

    属性:
        provider_id: 被测试的厂商标识（日志与响应回填用）。
        success: 测试是否成功（成功时 ``error_code`` / ``error_message`` 均为 None）。
        elapsed_ms: 测试耗时（毫秒），供 UI 展示「响应速度」与排查慢请求。
    """

    provider_id: int
    success: bool
    elapsed_ms: int
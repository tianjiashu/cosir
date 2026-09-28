"""模型连接测试结果值对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelConfigTestResult:
    """描述一次真实 OpenAI 兼容请求的脱敏结果。"""

    config_id: int | None
    success: bool
    elapsed_ms: int | None
    error_code: str | None = None
    error_message: str | None = None

"""环境文件单字段变更请求体。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class EnvironmentChangeRequest(BaseModel):
    """校验环境文件中单个字段的变更意图。

    参数:
        operation: 变更类型：``replace`` 覆盖、``clear`` 清空、``unchanged`` 保持不变。
        value: 新值；仅 ``replace`` 时使用，``clear`` / ``unchanged`` 忽略。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 字段类型不符、``operation`` 不在枚举内或携带额外字段时由 Pydantic 校验抛出。

    副作用:
        无（只做结构校验，不读写文件）。
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["replace", "clear", "unchanged"]
    value: Any = None

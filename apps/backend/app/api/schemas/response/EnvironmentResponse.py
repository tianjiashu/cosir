"""环境文件读取/更新响应结构。"""

from typing import Any

from pydantic import BaseModel


class EnvironmentResponse(BaseModel):
    """校验并序列化环境文件读取与更新结果。

    参数:
        fields: 环境字段的展示列表，每项为字段元数据映射（不含 secret 明文）。
        restart_required: 是否需要重启后生效；环境配置保存后会在当前进程内重载，默认 False。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    fields: list[dict[str, Any]]
    restart_required: bool = False

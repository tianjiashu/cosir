"""变更集操作请求模型（撤销 / 保留共用）。"""

from pydantic import BaseModel, Field


class ChangeSetActionRequest(BaseModel):
    """对一批文件执行撤销或保留的请求体。

    参数:
        paths: 相对 workspace 的文件路径列表，至少一项；长度为 1 即单文件操作。
    """

    paths: list[str] = Field(min_length=1)

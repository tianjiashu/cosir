from pydantic import BaseModel


class WorkspaceResponse(BaseModel):
    """校验并序列化工作区状态响应（与 ``WorkspaceRecord.to_dict()`` 对齐）。

    参数:
        workspace_id: 工作区标识。
        name: 工作区名称。
        root_path: 工作区根目录路径。
        created_at: 创建时间文本。
        updated_at: 更新时间文本。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    workspace_id: str
    name: str
    root_path: str
    created_at: str
    updated_at: str

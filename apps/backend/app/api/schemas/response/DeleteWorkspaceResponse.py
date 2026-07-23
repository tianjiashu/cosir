from pydantic import BaseModel


class DeleteWorkspaceResponse(BaseModel):
    """校验并序列化工作区删除结果响应。

    参数:
        workspace_id: 被删除的工作区标识。
        deleted: 是否删除成功。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    workspace_id: str
    deleted: bool

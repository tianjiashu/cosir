from pydantic import BaseModel, field_validator


class CreateWorkspaceRequest(BaseModel):
    """校验工作区创建请求体。

    参数:
        name: 非空的工作区名称。
        root_path: 非空的工作区本地路径。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当名称或路径为空白时抛出。

    副作用:
        无。
    """

    name: str
    root_path: str

    @field_validator("name", "root_path")
    @classmethod
    def text_fields_must_not_be_blank(cls, value: str) -> str:
        """校验工作区文本字段不为空白。

        参数:
            value: 从请求体解析出的字段值。

        返回:
            去除首尾空白后的字段值。

        异常:
            ValueError: 当字段为空白时抛出。

        副作用:
            无。
        """

        if not value.strip():
            raise ValueError("workspace field must not be blank")
        return value.strip()

from typing import ClassVar
from pydantic import BaseModel, field_validator


class CreateTurnRequest(BaseModel):
    """校验轮次创建请求体。

    参数:
        input_text: 非空的本轮用户输入。
        agent_id: 可选，指定执行本 turn 的 agent profile id；None 跟随默认。
        model_name: 可选，本 turn 请求的模型名（设计 §8.3，D1 配套）；None 表示
            Auto（跟随 Agent 默认模型）。该值经 ``turn_service.create_turn`` 透传
            落库到 ``turns.model_name``。
        paths: 可选，本 turn 关联的本地文件路径列表（仅做输入合法性校验，
            workspace 边界由下游工具层 ``PathResolver`` 强制）。可以是图片、文件、目录等。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当输入为空白或路径列表非法时抛出。

    副作用:
        无。
    """

    MAX_PATHS: ClassVar[int] = 5
    MAX_PATH_LEN: ClassVar[int] = 4096

    input_text: str
    agent_id: str | None = None
    model_name: str | None = None
    paths: list[str] | None = None

    @field_validator("input_text")
    @classmethod
    def input_text_must_not_be_blank(cls, value: str) -> str:
        """校验本轮输入不为空白。

        参数:
            value: 从请求体解析出的输入文本。

        返回:
            原始输入文本。

        异常:
            ValueError: 当输入为空白时抛出。

        副作用:
            无。
        """

        if not value or not value.strip():
            raise ValueError("input_text must not be blank")
        value = value.strip()
        if len(value) > 1000:
            raise ValueError("input_text must be less than 1000 characters")
        return value

    @field_validator("paths")
    @classmethod
    def paths_validator(cls, paths: list[str] | None) -> list[str] | None:
        """校验本轮文件路径列表的合法性与预算约束。

        复用 :meth:`_validate_path_list` 完成空值、空白项、项数上限与单条长度上限
        的统一校验，保证与 ``image_paths`` 口径一致。

        参数:
            file_paths: 从请求体解析出的文件路径列表。

        返回:
            校验通过的文件路径列表。

        异常:
            ValueError: 当列表为空、含空白项、项数超过上限或单条路径超长时抛出。

        副作用:
            无。
        """

        if paths is None:
            return paths
        if not paths or not all(v.strip() for v in paths):
            raise ValueError("paths must not be blank")
        if len(paths) > cls.MAX_PATHS:
            raise ValueError("paths must be less than 5 items")
        for v in paths:
            if len(v) > cls.MAX_PATH_LEN:
                raise ValueError(
                    "paths item must be less than 4096 characters"
                )
        return paths

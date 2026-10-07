"""Agent 最终结构化输出契约。"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class StructuredOutputSpec(BaseModel):
    """声明 Agent 最终结果必须满足的 JSON Schema。

    配置随 Agent profile 加载，并在执行期随 profile 快照传递。模型调用使用其名称和
    ``json_schema`` 构建临时结构化输出请求；节点仍会在本地执行 JSON Schema 校验，只有
    校验通过的 JSON 字符串才写入 ConversationRun.final_output。

    本模型只承载输出契约，不创建模型、不访问数据库，也不负责重试或 Run 状态迁移。

    参数:
        name: 提交给模型供应商的稳定 schema 名称。
        json_schema: Draft 2020-12 JSON Schema 对象。

    异常:
        ValueError: schema 名称或 JSON Schema 本身无效。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    json_schema: dict[str, Any] = Field(min_length=1)

    @field_validator("json_schema")
    @classmethod
    def validate_json_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        """校验 Draft 2020-12 schema 自身。

        参数:
            value: 待校验的 JSON Schema 对象。

        返回:
            原样返回通过元 schema 校验的对象。

        异常:
            jsonschema.exceptions.SchemaError: schema 自身不符合 Draft 2020-12。

        副作用:
            无。
        """

        Draft202012Validator.check_schema(value)
        return value

    def to_document(self) -> dict[str, Any]:
        """返回可持久化、可传递的 schema 契约字典。

        返回:
            含 ``name`` 与 ``json_schema`` 的 JSON 序列化兼容字典。

        异常:
            无。

        副作用:
            不写存储；返回的 schema 内容与本对象共享其内部字典，不应由调用方原地修改。
        """

        return {"name": self.name, "json_schema": self.json_schema}

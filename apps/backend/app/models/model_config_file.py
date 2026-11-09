"""模型配置文件（``<系统 .cosir>/config/models.json``）的数据契约。

文件是连接与其下模型的唯一事实源，用户可以直接手改；读取时按本契约严格校验（多余字段、重复的
连接 id / 连接名、同一连接下重复的模型名都拒绝），所以手改出错会在读取边界被指出，不会带着脏数据
进入运行期。

模型 id 是「连接 id/模型名」：连接 id 在创建时生成、之后不随名称变化，模型名不可改，所以 id 稳定；
连接 id 不含 ``/``，按第一个 ``/`` 拆分即可，模型名自身带 ``/``（如 ``deepseek-ai/DeepSeek-V3``）
不受影响。
删除后再加回同名模型得到同一个 id，引用它的 Agent 配置与历史 Run 会自动接上。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONNECTION_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class ModelEntry(BaseModel):
    """连接下的一个模型及用户声明的能力；能力不从模型名推断，不写即为 ``false``。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    model_name: str = Field(min_length=1)
    context_window_k: int = Field(gt=0)
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_image: bool = False


class ModelConnectionEntry(BaseModel):
    """一个 OpenAI 兼容接口的访问凭据及其下模型；Key 只在本机文件与内部调用链里流动。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str = Field(pattern=CONNECTION_ID_PATTERN)
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1, repr=False)
    models: tuple[ModelEntry, ...] = ()

    @model_validator(mode="after")
    def model_names_unique(self) -> ModelConnectionEntry:
        names = [model.model_name for model in self.models]
        if len(names) != len(set(names)):
            raise ValueError(f"连接「{self.name}」下有重名模型")
        return self

    def model_config_id(self, model_name: str) -> str:
        return f"{self.id}/{model_name}"


class ModelConfigFile(BaseModel):
    """文件顶层：连接按文件顺序排列，选择器与默认选择都依赖这个顺序。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    connections: tuple[ModelConnectionEntry, ...] = ()

    @model_validator(mode="after")
    def connections_unique(self) -> ModelConfigFile:
        for field in ("id", "name"):
            values = [getattr(connection, field) for connection in self.connections]
            if len(values) != len(set(values)):
                raise ValueError(f"连接的 {field} 不能重复")
        return self

    def connection(self, connection_id: str) -> ModelConnectionEntry:
        """按 id 取连接，不存在时抛 ``KeyError``。"""

        for connection in self.connections:
            if connection.id == connection_id:
                return connection
        raise KeyError(connection_id)

    def find(self, model_config_id: str) -> tuple[ModelConnectionEntry, ModelEntry]:
        """按模型 id 取连接与模型，不存在（含格式不对）时抛 ``KeyError``。"""

        connection_id, _, model_name = model_config_id.partition("/")
        connection = self.connection(connection_id)
        for model in connection.models:
            if model.model_name == model_name:
                return connection, model
        raise KeyError(model_config_id)

    def new_connection_id(self, name: str) -> str:
        """由名称生成未被占用的连接 id（小写字母数字与 ``-``）。

        名称里没有可用字符（如全中文）时用 ``connection``。
        """

        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "connection"
        taken = {connection.id for connection in self.connections}
        candidate, suffix = base, 2
        while candidate in taken:
            candidate, suffix = f"{base}-{suffix}", suffix + 1
        return candidate

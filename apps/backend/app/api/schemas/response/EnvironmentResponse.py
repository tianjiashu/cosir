"""环境配置中心的分组展示响应结构。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class EnvironmentOptionResponse(BaseModel):
    """下拉控件的一个可选项。"""

    model_config = ConfigDict(extra="forbid")

    value: str
    label: str


class EnvironmentFieldResponse(BaseModel):
    """单个环境变量的展示元数据与脱敏状态。

    ``value`` 与 ``disk_value`` 对 secret 字段始终为 ``None``；调用方只能通过 ``masked``
    判断是否已经配置，不能从响应中恢复敏感值。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["string", "boolean"]
    component: Literal["input", "password", "checkbox", "select"]
    label: str
    secret: bool
    default: str | bool | None
    value: str | bool | None
    disk_value: str | bool | None
    configured: bool
    masked: bool
    source: Literal["process", "file", "default"]
    process_override: bool
    options: list[EnvironmentOptionResponse]
    placeholder: str | None
    clearable: bool


class EnvironmentGroupResponse(BaseModel):
    """配置中心中的一组相关环境变量。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    description: str
    fields: list[EnvironmentFieldResponse]


class EnvironmentResponse(BaseModel):
    """环境配置读取与更新响应。

    分组、字段名称和控件类型均由后端返回；响应不包含 version 等并发控制字段。
    保存后由后端就地把新配置重载进内存（见 ``EnvironmentConfigurationService``），因此没有
    「需要重启才生效」的字段。
    """

    model_config = ConfigDict(extra="forbid")

    groups: list[EnvironmentGroupResponse]

"""写入用户变量（白名单以外的 ``.env`` 键，如 MCP 配置引用的密钥）的请求体。"""

from pydantic import BaseModel, ConfigDict, Field


class EnvironmentVariablesUpdateRequest(BaseModel):
    """变量名到值；键名与值的规则由 ``EnvironmentConfigurationService.set_user_variables`` 校验。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    values: dict[str, str] = Field(min_length=1)

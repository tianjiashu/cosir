"""启用或停用一个 Skill 的请求体。"""

from pydantic import BaseModel, ConfigDict


class SkillEnabledUpdateRequest(BaseModel):
    """单个 Skill 的启用状态；只作用于请求路径所在的作用域。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool

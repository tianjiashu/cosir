"""一次启用或停用若干个 Skill 的请求体。"""

from pydantic import BaseModel, ConfigDict, Field


class SkillsEnabledUpdateRequest(BaseModel):
    """这些名称都要是请求路径所在作用域这一层的生效 Skill；单个启停就是只有一项的列表。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    names: list[str] = Field(min_length=1)
    enabled: bool

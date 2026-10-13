"""系统级 Skill 启用状态配置。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError

from app.config.logging.logger import log
from app.utils.path.system_cosir import system_skills_config_file


class SkillConfiguration(BaseModel):
    """由系统配置文件声明允许 Agent 使用的 Skill 名称。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled_skills: list[str] | None = None

    def is_enabled(self, name: str) -> bool:
        """配置文件缺失时默认开放已安装 Skill；存在时仅开放显式列出的名称。"""

        return self.enabled_skills is None or name in self.enabled_skills


class SkillConfigurationError(ValueError):
    """系统级 Skill 配置文件无法读取或不符合契约。"""


def read_skill_configuration() -> SkillConfiguration:
    """读取 Skill 启用状态；尚未创建配置文件时默认全部启用。"""

    path = system_skills_config_file()
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SkillConfiguration()
    except (OSError, UnicodeDecodeError) as exc:
        log.error(
            "skill_configuration_read_failed",
            extra={
                "msg": "读取 Skill 配置失败",
                "data": {"error_type": type(exc).__name__},
            },
        )
        raise SkillConfigurationError("Skill 配置文件无法读取") from exc

    try:
        configuration = SkillConfiguration.model_validate_json(content)
        if configuration.enabled_skills is None:
            raise ValueError("enabled_skills is required")
        return configuration
    except (ValidationError, ValueError) as exc:
        log.error(
            "skill_configuration_invalid",
            extra={
                "msg": "Skill 配置文件格式无效",
                "data": {"error_type": type(exc).__name__},
            },
        )
        raise SkillConfigurationError("Skill 配置文件格式无效") from exc

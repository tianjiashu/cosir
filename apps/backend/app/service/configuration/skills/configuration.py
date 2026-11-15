"""一个作用域的 Skill 启用状态配置（``<.cosir>/config/skills.json``）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.service.configuration import file_store
from app.service.configuration.extension_scope import ExtensionScope


class SkillConfiguration(BaseModel):
    """本作用域停用了哪些 Skill。

    用停用名单而不是启用名单：外部工具往 ``.agents/skills`` 新装的 Skill 不需要用户再逐个勾选就可用。
    名单是 cosir 自己的偏好，不写进共享的 Skill 目录。每个作用域只管自己的 Skill：workspace 的名单不能
    启停系统级的，反之亦然。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    disabled_skills: list[str] = []

    def skill_enabled(self, name: str) -> bool:
        return name not in self.disabled_skills

    @classmethod
    def read(cls, scope: ExtensionScope) -> SkillConfiguration:
        """读取一个作用域的 Skill 启用状态；尚未创建配置文件时全部取默认值。

        异常:
            SkillConfigurationError: 配置文件无法读取或格式无效。
        """

        return file_store.read_json_model(
            scope.skills_config_file,
            cls,
            error=SkillConfigurationError,
            label="Skill 配置",
            event="skill_configuration",
            log_data={"scope": scope.key},
        )


    def write(self, scope: ExtensionScope) -> None:
        """原子写回一个作用域的停用名单；调用方在 ``file_store.locked(scope.skills_config_file)`` 里读改写。"""

        file_store.write_json(scope.skills_config_file, self, root=scope.cosir_dir)


class SkillConfigurationError(ValueError):
    """Skill 启用状态配置文件无法读取或不符合契约。"""

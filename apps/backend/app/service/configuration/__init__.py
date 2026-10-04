"""系统与 workspace 配置 service。

本包承载系统级与 workspace `.cosir` 配置的文件事实读写与应用编排。API 只负责 HTTP schema
和错误映射，前端不直接访问本地文件。各配置类型保持独立 service，避免 Agent、Team、指令和
环境配置形成第二套共享状态机；Team 的静态领域模型仍位于 ``app.agent_team.configuration``。
"""

from app.service.configuration.file_store import (
    ConfigurationFileError,
    ConfigurationFileStore,
    ConfigurationPathError,
)

__all__ = [
    "ConfigurationFileError",
    "ConfigurationFileStore",
    "ConfigurationPathError",
]

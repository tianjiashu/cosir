"""系统配置文件 service。

本包承载系统级 `.cosir` 配置的文件事实读写与领域校验。API 只负责 HTTP schema 和错误映射，
前端不直接访问本地文件。各配置类型保持独立 service，避免 Agent、指令和环境配置形成第二套
共享状态机。
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

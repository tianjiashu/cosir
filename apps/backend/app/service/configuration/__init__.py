"""系统与 workspace 配置 service。

本包承载系统级与 workspace `.cosir` 配置的文件事实读写与应用编排，按能力分子包（``agents/``、
``instructions/``、``mcp/``、``skills/``），跨能力共用的放在根上（文件读写 ``file_store``、Skill 与 MCP
共用的作用域 ``extension_scope``）。API 只负责 HTTP schema 和错误映射，前端不直接访问本地文件。
各配置类型保持独立 service，避免形成第二套共享状态机。
"""

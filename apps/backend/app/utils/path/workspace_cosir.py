"""Workspace 级 ``.cosir`` 目录路径计算（leaf 层纯路径工具）。

单一职责：给出 workspace 内 ``.cosir`` 目录及其子目录（附件、工具产物、子 Agent 配置等）
的路径。只做路径运算，**不创建目录、不读写文件、不依赖业务模型**。

为什么集中：``.cosir`` 基名与子目录规则此前在 ``workspace_service``、``attachment_service``、
``image_utils`` 三处各自拼接，口径容易漂移；本模块是唯一允许拼接 ``.cosir`` 基名的位置。

不负责：目录创建（见 ``WorkspaceService``）、附件与工具产物 artifact 的实际读写、系统级
路径落点（见 ``app.utils.path.system_cosir``）。
"""

from __future__ import annotations

from pathlib import Path

COSIR_DIR_NAME: str = ".cosir"
COSIR_ATTACHMENT_DIR_NAME: str = "Attachment"
COSIR_ATTACHMENT_STAGING_DIR_NAME: str = ".uploading"
COSIR_TOOL_ARTIFACT_DIR_NAME: str = "tool-artifacts"
COSIR_AGENT_CONFIG_DIR_NAME: str = "agents"
COSIR_AGENT_TEAM_CONFIG_DIR_NAME: str = "agent-teams"
COSIR_INSTRUCTION_FILE_NAME: str = "AGENTS.md"
COSIR_MAIN_AGENT_PROMPT_FILE_NAME: str = "main_agent_system_prompt.md"
COSIR_ENV_FILE_NAME: str = ".env"


def workspace_cosir_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 内 ``.cosir`` 目录路径。

    参数:
        workspace_root: 工作区根目录（字符串或 ``Path``）。

    返回:
        ``<workspace_root>/.cosir``；不做 ``resolve``、不校验存在性。

    异常:
        无。

    副作用:
        无（纯路径拼接，不访问文件系统）。
    """

    return Path(workspace_root) / COSIR_DIR_NAME


def workspace_attachment_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 附件目录路径（``<workspace>/.cosir/Attachment``）。"""

    return workspace_cosir_dir(workspace_root) / COSIR_ATTACHMENT_DIR_NAME


def workspace_attachment_staging_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 附件上传暂存目录路径（``<workspace>/.cosir/Attachment/.uploading``）。"""

    return workspace_attachment_dir(workspace_root) / COSIR_ATTACHMENT_STAGING_DIR_NAME


def workspace_tool_artifact_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 工具输出 artifact 目录路径（``<workspace>/.cosir/tool-artifacts``）。"""

    return workspace_cosir_dir(workspace_root) / COSIR_TOOL_ARTIFACT_DIR_NAME


def workspace_agent_config_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 子 Agent JSON 配置目录路径（``<workspace>/.cosir/agents``）。"""

    return workspace_cosir_dir(workspace_root) / COSIR_AGENT_CONFIG_DIR_NAME


def workspace_agent_team_config_dir(workspace_root: str | Path) -> Path:
    """返回 workspace Team JSON 配置目录路径。"""

    return workspace_cosir_dir(workspace_root) / COSIR_AGENT_TEAM_CONFIG_DIR_NAME


__all__ = [
    "COSIR_AGENT_CONFIG_DIR_NAME",
    "COSIR_AGENT_TEAM_CONFIG_DIR_NAME",
    "COSIR_ATTACHMENT_DIR_NAME",
    "COSIR_ATTACHMENT_STAGING_DIR_NAME",
    "COSIR_DIR_NAME",
    "COSIR_ENV_FILE_NAME",
    "COSIR_INSTRUCTION_FILE_NAME",
    "COSIR_MAIN_AGENT_PROMPT_FILE_NAME",
    "COSIR_TOOL_ARTIFACT_DIR_NAME",
    "workspace_agent_config_dir",
    "workspace_agent_team_config_dir",
    "workspace_attachment_dir",
    "workspace_attachment_staging_dir",
    "workspace_cosir_dir",
    "workspace_tool_artifact_dir",
]

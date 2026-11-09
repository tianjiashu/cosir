"""Workspace 级 ``.cosir`` 目录路径计算（leaf 层纯路径工具）。

单一职责：给出 workspace 内 ``.cosir`` 目录及其子目录与文件（附件、浏览器下载、工具产物、
Agent 配置、Skill、MCP 配置、文件面板设置和 Team 运行档案等）的路径。只做路径运算，
**不创建目录、不读写文件、不依赖业务模型**。

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
COSIR_DOWNLOAD_DIR_NAME: str = "Download"
COSIR_TOOL_ARTIFACT_DIR_NAME: str = "tool-artifacts"
COSIR_AGENT_CONFIG_DIR_NAME: str = "agents"
COSIR_AGENT_TEAM_CONFIG_DIR_NAME: str = "agent-teams"
COSIR_AGENT_TEAM_RUNS_DIR_NAME: str = "agent-team-runs"
COSIR_INSTRUCTION_FILE_NAME: str = "AGENTS.md"
COSIR_MAIN_AGENT_PROMPT_FILE_NAME: str = "main_agent_system_prompt.md"
COSIR_ENV_FILE_NAME: str = ".env"
# Skill 与 MCP 的配置布局在系统级与 workspace 级 ``.cosir`` 下完全一致，名字只在这里定义一次。
COSIR_CONFIG_DIR_NAME: str = "config"
COSIR_SKILLS_DIR_NAME: str = "skills"
COSIR_SKILLS_CONFIG_FILE_NAME: str = "skills.json"
COSIR_MCP_SERVERS_FILE_NAME: str = "mcp_servers.json"
COSIR_FILE_BROWSER_SETTINGS_FILE_NAME: str = "file-browser.json"


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


def workspace_download_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 下载目录路径（``<workspace>/.cosir/Download``）：内置浏览器的下载落盘处。"""

    return workspace_cosir_dir(workspace_root) / COSIR_DOWNLOAD_DIR_NAME


def workspace_tool_artifact_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 工具输出 artifact 目录路径（``<workspace>/.cosir/tool-artifacts``）。"""

    return workspace_cosir_dir(workspace_root) / COSIR_TOOL_ARTIFACT_DIR_NAME


def workspace_agent_config_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 子 Agent JSON 配置目录路径（``<workspace>/.cosir/agents``）。"""

    return workspace_cosir_dir(workspace_root) / COSIR_AGENT_CONFIG_DIR_NAME


def workspace_agent_team_config_dir(workspace_root: str | Path) -> Path:
    """返回 workspace Team JSON 配置目录路径。"""

    return workspace_cosir_dir(workspace_root) / COSIR_AGENT_TEAM_CONFIG_DIR_NAME


def workspace_agent_team_run_dir(workspace_root: str | Path) -> Path:
    """返回 workspace Agent Team 运行档案目录路径。"""

    return workspace_cosir_dir(workspace_root) / COSIR_AGENT_TEAM_RUNS_DIR_NAME


def workspace_file_browser_settings_file(workspace_root: str | Path) -> Path:
    """返回文件面板显示设置文件路径（``<workspace>/.cosir/file-browser.json``）。"""

    return workspace_cosir_dir(workspace_root) / COSIR_FILE_BROWSER_SETTINGS_FILE_NAME


def workspace_skills_dir(workspace_root: str | Path) -> Path:
    """返回 workspace Skill 目录路径（``<workspace>/.cosir/skills``）。"""

    return workspace_cosir_dir(workspace_root) / COSIR_SKILLS_DIR_NAME


def workspace_skills_config_file(workspace_root: str | Path) -> Path:
    """返回 workspace Skill 启用状态文件路径（``<workspace>/.cosir/config/skills.json``）。"""

    config_dir = workspace_cosir_dir(workspace_root) / COSIR_CONFIG_DIR_NAME
    return config_dir / COSIR_SKILLS_CONFIG_FILE_NAME


def workspace_mcp_servers_file(workspace_root: str | Path) -> Path:
    """返回 workspace MCP 服务配置文件路径（``<workspace>/.cosir/config/mcp_servers.json``）。

    文件可能随仓库共享，只放连接定义；「本机测试通过」记录在系统级，见
    ``system_cosir.system_mcp_tested_servers_file``。
    """

    return workspace_cosir_dir(workspace_root) / COSIR_CONFIG_DIR_NAME / COSIR_MCP_SERVERS_FILE_NAME


__all__ = [
    "COSIR_AGENT_CONFIG_DIR_NAME",
    "COSIR_AGENT_TEAM_CONFIG_DIR_NAME",
    "COSIR_AGENT_TEAM_RUNS_DIR_NAME",
    "COSIR_ATTACHMENT_DIR_NAME",
    "COSIR_ATTACHMENT_STAGING_DIR_NAME",
    "COSIR_CONFIG_DIR_NAME",
    "COSIR_DIR_NAME",
    "COSIR_DOWNLOAD_DIR_NAME",
    "COSIR_ENV_FILE_NAME",
    "COSIR_FILE_BROWSER_SETTINGS_FILE_NAME",
    "COSIR_INSTRUCTION_FILE_NAME",
    "COSIR_MAIN_AGENT_PROMPT_FILE_NAME",
    "COSIR_MCP_SERVERS_FILE_NAME",
    "COSIR_SKILLS_CONFIG_FILE_NAME",
    "COSIR_SKILLS_DIR_NAME",
    "COSIR_TOOL_ARTIFACT_DIR_NAME",
    "workspace_agent_config_dir",
    "workspace_agent_team_config_dir",
    "workspace_agent_team_run_dir",
    "workspace_attachment_dir",
    "workspace_attachment_staging_dir",
    "workspace_cosir_dir",
    "workspace_download_dir",
    "workspace_file_browser_settings_file",
    "workspace_mcp_servers_file",
    "workspace_skills_config_file",
    "workspace_skills_dir",
    "workspace_tool_artifact_dir",
]

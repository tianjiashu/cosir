"""路径工具子包：系统级 / workspace 级 ``.cosir`` 路径与路径校验。

- 系统级固定路径与 ``.cosir`` 子目录：``app.utils.path.system_cosir``
- workspace 级 ``.cosir`` 路径：``app.utils.path.workspace_cosir``
- 路径校验（保留子树判定）：``app.utils.path.validation``
"""

from app.utils.path.system_cosir import (
    CHECKPOINT_FILE,
    DATA_DIR,
    DATABASE_FILE,
    LOG_DIR,
    LOG_FILE_NAME,
    RUNTIME_DIR,
    SYSTEM_COSIR_DIR,
    env_file,
    override,
    reset,
    system_agent_config_dir,
    system_cosir_dir,
    system_env_file,
    system_instruction_file,
    system_main_agent_prompt_file,
)
from app.utils.path.workspace_cosir import (
    COSIR_AGENT_CONFIG_DIR_NAME,
    COSIR_ATTACHMENT_DIR_NAME,
    COSIR_ATTACHMENT_STAGING_DIR_NAME,
    COSIR_DIR_NAME,
    COSIR_ENV_FILE_NAME,
    COSIR_INSTRUCTION_FILE_NAME,
    COSIR_MAIN_AGENT_PROMPT_FILE_NAME,
    COSIR_TOOL_ARTIFACT_DIR_NAME,
    workspace_agent_config_dir,
    workspace_attachment_dir,
    workspace_attachment_staging_dir,
    workspace_cosir_dir,
    workspace_tool_artifact_dir,
)
from app.utils.path.validation import is_within_cosir

__all__ = [
    "DATA_DIR",
    "SYSTEM_COSIR_DIR",
    "LOG_DIR",
    "DATABASE_FILE",
    "CHECKPOINT_FILE",
    "RUNTIME_DIR",
    "LOG_FILE_NAME",
    "reset",
    "override",
    "env_file",
    "system_cosir_dir",
    "system_instruction_file",
    "system_main_agent_prompt_file",
    "system_agent_config_dir",
    "system_env_file",
    "COSIR_DIR_NAME",
    "COSIR_ATTACHMENT_DIR_NAME",
    "COSIR_ATTACHMENT_STAGING_DIR_NAME",
    "COSIR_TOOL_ARTIFACT_DIR_NAME",
    "COSIR_AGENT_CONFIG_DIR_NAME",
    "COSIR_INSTRUCTION_FILE_NAME",
    "COSIR_MAIN_AGENT_PROMPT_FILE_NAME",
    "COSIR_ENV_FILE_NAME",
    "workspace_cosir_dir",
    "workspace_attachment_dir",
    "workspace_attachment_staging_dir",
    "workspace_tool_artifact_dir",
    "workspace_agent_config_dir",
    "is_within_cosir",
]

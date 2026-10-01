"""系统级子 Agent 配置目录的确保创建。

本模块只负责在启动期确保系统 ``.cosir/agents`` 目录存在，供用户放置自定义子 Agent
JSON。它不随包分发默认配置、不读取或校验 Agent 配置内容：JSON schema 校验与 profile
构造统一由 ``app.core.agents.agent_profile.AgentProfile.vaild_agent_profile`` 持有。
"""

from __future__ import annotations

from pathlib import Path

from app.core.agents.agent_profile import AgentProfileConfigError
from app.utils.cosir_paths import system_agent_config_dir


def ensure_system_agent_config_dir() -> Path:
    """确保系统级子 Agent 配置目录存在。

    启动期调用，保证后续 ``AgentProfileRegistry.load_agent_profiles`` 能从该目录读取用户
    自定义子 Agent JSON。目录已存在时幂等返回。

    返回:
        系统级 Agent 配置目录。

    异常:
        AgentProfileConfigError: 目录创建失败（权限不足、路径是失效符号链接等）。

    副作用:
        必要时创建系统 ``.cosir/agents`` 目录；不写入默认配置、不读取或校验内容。
    """

    directory: Path = system_agent_config_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AgentProfileConfigError(
            f"系统 Agent 配置目录创建失败，目录={directory}，"
            f"原因={type(exc).__name__}: {exc}"
        ) from exc
    return directory

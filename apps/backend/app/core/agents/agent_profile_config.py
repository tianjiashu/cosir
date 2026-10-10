"""系统级 Agent 与 Agent Team 配置目录的确保创建。

本模块只负责在启动期确保系统 ``.cosir/agents`` 与 ``.cosir/agent-teams`` 目录存在，供用户放置
自定义子 Agent / Team JSON。它不随包分发默认配置、不读取或校验配置内容：JSON schema 校验与
profile 构造统一由 ``app.core.agents.agent_profile.AgentProfile.vaild_agent_profile`` 持有，
Team 配置校验由 ``AgentTeamConfiguration`` 领域模型持有。

为什么需要独立于装载：装载是「读目录」，目录不存在时按无配置处理即可；而「目录存在」是用户
可发现、可手工放置配置的前提，属于启动期的一次性 provisioning，不能依赖装载的副作用。
"""

from __future__ import annotations

from pathlib import Path

from app.core.agents.agent_profile import AgentProfileConfigError
from app.core.agents.agent_profile_source import AgentProfileScopeSource
from app.utils.path.system_cosir import system_agent_team_config_dir
from app.utils.workspace_scope import SYSTEM_SCOPE


def ensure_system_agent_config_dir() -> Path:
    """确保系统级子 Agent 配置目录存在。

    启动期调用，保证用户有固定位置放置自定义子 Agent JSON。目录已存在时幂等返回。

    返回:
        系统级 Agent 配置目录。

    异常:
        AgentProfileConfigError: 目录创建失败（权限不足、路径是失效符号链接等）。

    副作用:
        必要时创建系统 ``.cosir/agents`` 目录；不写入默认配置、不读取或校验内容。
    """

    # 目录取自配置来源：系统作用域的目录定义只保留一处，避免 provisioning 与装载各算一遍。
    return _ensure_directory(
        AgentProfileScopeSource().directory(SYSTEM_SCOPE),
        failure_message="系统 Agent 配置目录创建失败",
    )


def ensure_system_agent_team_config_dir() -> Path:
    """确保系统级 Agent Team 配置目录存在。

    与 :func:`ensure_system_agent_config_dir` 同理：启动期保证用户有固定位置放置自定义 Team
    JSON。目录已存在时幂等返回。

    返回:
        系统级 Agent Team 配置目录。

    异常:
        AgentProfileConfigError: 目录创建失败（权限不足、路径是失效符号链接等）。

    副作用:
        必要时创建系统 ``.cosir/agent-teams`` 目录；不写入默认配置、不读取或校验内容。
    """

    return _ensure_directory(
        system_agent_team_config_dir(),
        failure_message="系统 Agent Team 配置目录创建失败",
    )


def _ensure_directory(directory: Path, *, failure_message: str) -> Path:
    """幂等创建配置目录，失败时统一包装为配置错误。

    参数:
        directory: 待创建的配置目录。
        failure_message: 失败消息前缀，用于区分是哪个目录创建失败。

    返回:
        创建后的目录路径。

    异常:
        AgentProfileConfigError: ``OSError`` 时包装抛出，消息包含目录与原因。

    副作用:
        必要时创建目录（含父目录）。
    """

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AgentProfileConfigError(
            f"{failure_message}，目录={directory}，原因={type(exc).__name__}: {exc}"
        ) from exc
    return directory

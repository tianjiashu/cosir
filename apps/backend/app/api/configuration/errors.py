"""系统配置中心各子域共用的 HTTP 错误映射。

三个子域的领域异常类不同，但对外错误形状必须一致：本模块是「领域异常 → HTTP 状态码与错误码」
的唯一映射点，避免每个路由模块各写一份。各路由模块以 ``except Exception as exc:
raise_configuration_error(exc)`` 的形态调用。

本模块只做映射与抛出：不读写配置文件、不构造成功响应、不装配 service 依赖。
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException

from app.core.agents.agent_profile import AgentProfileConfigError
from app.service.configuration.agent_configuration_service import AgentConfigurationError
from app.service.configuration.environment_configuration_service import (
    EnvironmentConfigurationError,
)
from app.service.configuration.file_store import (
    ConfigurationFileError,
    ConfigurationPathError,
)
from app.service.configuration.instruction_configuration_service import (
    InstructionConfigurationError,
)
from app.service.configuration.main_agent_prompt_configuration_service import (
    MainAgentPromptConfigurationError,
)
from app.service.configuration.workspace_fileignore_configuration_service import (
    WorkspaceFileIgnoreConfigurationError,
)
from app.service.configuration.workspace_instruction_configuration_service import (
    WorkspaceInstructionConfigurationError,
)


def raise_configuration_error(exc: Exception) -> NoReturn:
    """把配置中心抛出的异常映射为 ``HTTPException``。

    参数:
        exc: 路由函数捕获到的原始异常。

    返回:
        无返回；签名标注 ``NoReturn``，所有分支都以抛出结束。

    异常:
        HTTPException: 404（``KeyError``，目标配置不存在）、409（``FileExistsError``，
            同名配置已存在）、400（``ConfigurationPathError`` / ``AgentConfigurationError`` /
            ``InstructionConfigurationError`` / ``EnvironmentConfigurationError`` /
            ``AgentProfileConfigError`` / ``ValueError``，请求内容或路径不合法）、
            500（``PermissionError``，本机文件不可写；``ConfigurationFileError`` / ``OSError``，
            配置文件读写失败）。
        Exception: 未匹配的异常原样重抛，交给全局异常处理中间件。
    """

    if isinstance(exc, KeyError):
        raise HTTPException(
            status_code=404, detail={"code": "configuration_not_found", "message": "配置不存在"}
        ) from exc
    if isinstance(exc, FileExistsError):
        raise HTTPException(
            status_code=409, detail={"code": "configuration_conflict", "message": str(exc)}
        ) from exc
    if isinstance(
        exc,
        ConfigurationPathError
        | AgentConfigurationError
        | InstructionConfigurationError
        | MainAgentPromptConfigurationError
        | WorkspaceInstructionConfigurationError
        | WorkspaceFileIgnoreConfigurationError
        | EnvironmentConfigurationError
        | AgentProfileConfigError
        | ValueError,
    ):
        raise HTTPException(
            status_code=400, detail={"code": "configuration_invalid", "message": str(exc)}
        ) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(
            status_code=500,
            detail={"code": "configuration_permission_denied", "message": "配置文件不可写"},
        ) from exc
    if isinstance(exc, ConfigurationFileError | OSError):
        raise HTTPException(
            status_code=500,
            detail={"code": "configuration_io_failed", "message": "配置文件读写失败"},
        ) from exc
    raise exc

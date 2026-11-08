"""系统配置中心各子域共用的 HTTP 错误映射。

各配置子域的领域异常类不同，但对外错误形状必须一致：本模块是「领域异常 → HTTP 状态码与错误码」
的唯一映射点，避免每个路由模块各写一份。各路由模块以 ``except Exception as exc:
raise_configuration_error(exc)`` 的形态调用。

本模块只做映射与抛出：不读写配置文件、不构造成功响应、不装配 service 依赖。
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException


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
    # 各配置子域的校验异常（含路径越界）都是 ValueError 子类，统一映射为 400。
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=400, detail={"code": "configuration_invalid", "message": str(exc)}
        ) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(
            status_code=500,
            detail={"code": "configuration_permission_denied", "message": "配置文件不可写"},
        ) from exc
    # 读写失败（``ConfigurationFileError`` 是 OSError 子类）。
    if isinstance(exc, OSError):
        raise HTTPException(
            status_code=500,
            detail={"code": "configuration_io_failed", "message": "配置文件读写失败"},
        ) from exc
    raise exc

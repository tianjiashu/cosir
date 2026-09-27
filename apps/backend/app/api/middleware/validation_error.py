"""FastAPI 请求校验错误的响应形状处理器。

FastAPI 的 ``exception_handlers`` 按异常**类型**索引，同一类型只能有一个生效处理器；
本模块是 ``RequestValidationError`` 的唯一注册者，因此必须为未定制路径保留默认 422 形状，
否则会静默改变其它 API（含 Assistant Transport）的错误契约。
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def install_request_validation_error_handler(app: FastAPI) -> None:
    """注册 ``RequestValidationError`` 全局处理器，按路径族定制校验错误响应形状。

    ``/configuration/`` 下的请求返回 400，且 ``detail`` 为 ``{code, message, errors}``，
    供前端配置中心按 ``code`` 分支；其余路径保持 FastAPI 默认的 422 与
    ``{"detail": exc.errors()}`` 形状，使既有 API 的错误契约零改动。

    注意：FastAPI 的异常处理器按异常类型唯一。本函数是该类型的唯一注册者；新增同类型
    处理器会静默覆盖本处理器（或反之），且非配置路径的 422 兜底必须保留。

    参数:
        app: FastAPI 应用实例。

    返回:
        无。

    异常:
        无。

    副作用:
        在 FastAPI 应用上注册 ``RequestValidationError`` 的全局处理器，覆盖框架默认实现；
        不触碰 storage 或 service。
    """

    @app.exception_handler(RequestValidationError)
    async def configuration_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """按请求路径把校验错误映射为配置中心或框架默认的错误体。

        参数:
            request: 触发校验失败的 HTTP 请求，仅读取其路径。
            exc: FastAPI 校验异常，携带 ``errors()`` 明细。

        返回:
            ``/configuration/`` 路径返回 400 与配置中心错误体；其余路径返回 422 与框架默认
            ``{"detail": [...]}`` 形状。

        异常:
            无。

        副作用:
            无；仅构造响应，不触碰 storage 或 service。
        """

        if request.url.path.startswith("/configuration/"):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": {
                        "code": "configuration_invalid",
                        "message": "配置请求参数无效",
                        "errors": exc.errors(),
                    }
                },
            )
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

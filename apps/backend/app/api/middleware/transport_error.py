"""Assistant Transport 纯 wire 契约校验异常处理器。"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.transport.assistant_transport_request import TransportRequestError


def install_transport_request_error_handler(app: FastAPI) -> None:
    """注册 ``TransportRequestError`` 全局异常处理器，映射为统一错误体。

    把 ``AssistantTransportRequest`` 经 ``model_validator`` 自动抛出的纯 wire 契约校验异常，
    转换为与 ``assistant_api._raise_transport_error`` 完全相同的
    ``{error:{code,message,retryable}}`` 形状，使前端 ``TransportError`` 契约零改动；同时复用
    自定义 ``status_code``，不会退化成 Pydantic 422（422 会破坏前端 ``assistant-stream``
    客户端的 TransportError 解析）。

    参数:
        app: FastAPI 应用实例。

    返回:
        无。

    异常:
        无。

    副作用:
        在 FastAPI 应用上注册 ``TransportRequestError`` 的全局异常处理器，覆盖默认的
        校验错误响应；不触碰 storage 或 service。
    """

    @app.exception_handler(TransportRequestError)
    async def _handle_transport_request_error(
        _request: object,
        exc: TransportRequestError,
    ) -> JSONResponse:
        """把纯 wire 契约校验异常映射为统一的 Assistant Transport HTTP 错误体。

        参数:
            _request: 触发异常的 HTTP 请求，本处理器不消费其内容。
            exc: 携带 ``status_code`` / ``code`` / ``message`` / ``retryable`` 的校验异常。

        返回:
            含统一错误体的 ``JSONResponse``，状态码取自 ``exc.status_code``。

        异常:
            无。

        副作用:
            无；仅构造响应，不触碰 storage 或 service。
        """
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                }
            },
        )

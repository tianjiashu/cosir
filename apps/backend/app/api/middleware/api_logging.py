"""FastAPI 请求日志 middleware。"""

import json
import logging
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.config.logging import merge_log_context, reset_log_context
from app.trace_infra import is_trace_id, new_trace_id


def install_request_logging(app, logger: logging.Logger) -> None:
    """为 FastAPI 应用安装统一请求日志。

    参数:
        app: FastAPI 应用实例。
        logger: 后端结构化日志器。

    返回:
        无。

    异常:
        无。middleware 内部会记录未捕获异常并继续抛出。

    副作用:
        在 FastAPI 应用上注册一个 HTTP middleware。
    """

    @app.middleware("http")
    async def request_logging_middleware(request, call_next):
        """记录单个 HTTP 请求的入口、完成、已处理失败或未捕获异常。

        参数:
            request: Starlette 请求对象。
            call_next: FastAPI 提供的下一个请求处理器。

        返回:
            下游处理器返回的响应对象。

        异常:
            Exception: 下游未捕获异常会在写入 ``unhandled_exception`` 后继续抛出。

        副作用:
            写入 ``http_request_started``、``http_request_finished``、
            ``http_request_failed`` 或 ``http_unhandled_exception`` 结构化日志。
        """

        trace_id = _request_trace_id(request.headers.get("x-trace-id", ""))
        started_at = time.perf_counter()
        # 入口层仅绑定 trace_id（日志层唯一链路键）；实体 ID 经 extra 落入 data。
        token = merge_log_context(trace_id=trace_id)
        extra = await _request_log_extra(request)
        try:
            try:
                logger.info("http_request_started", extra={**extra, "msg": "HTTP 请求开始"})
                response = await call_next(request)
            except Exception:
                duration_ms = _duration_ms(started_at)
                logger.exception(
                    "http_unhandled_exception",
                    extra={
                        **extra,
                        "duration_ms": duration_ms,
                        "status_code": 500,
                        "msg": "HTTP 请求处理未捕获异常",
                    },
                )
                raise
            duration_ms = _duration_ms(started_at)
            response.headers.setdefault("x-trace-id", trace_id)
            completed_extra = {
                **extra,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            }
            # 已由全局 HTTPException 处理器记录 detail 时，避免重复打印失败日志。
            already_logged = getattr(request.state, "exception_logged", False)
            if not already_logged and response.status_code >= 400:
                if response.status_code >= 500:
                    logger.error(
                        "http_request_failed", extra={**completed_extra, "msg": "HTTP 请求失败"}
                    )
                else:
                    logger.warning(
                        "http_request_failed", extra={**completed_extra, "msg": "HTTP 请求失败"}
                    )
            elif response.status_code < 400:
                logger.info(
                    "http_request_finished", extra={**completed_extra, "msg": "HTTP 请求完成"}
                )
            return response
        finally:
            reset_log_context(token)


def install_http_exception_logging(app, logger: logging.Logger) -> None:
    """注册全局 HTTPException 处理器，集中记录业务异常的失败原因。

    所有端点抛出的 ``HTTPException``（含端点内把 ``ValueError`` / ``KeyError`` /
    ``RuntimeError`` 翻译而成的 400 / 404 / 503）都会在此统一记录 ``detail``，
    避免失败原因在日志中丢失。处理器运行在请求日志 middleware 的上下文内，因此
    自动继承 ``trace_id`` 等上下文字段，无需在每个端点里手写日志。

    参数:
        app: FastAPI 应用实例。
        logger: 后端结构化日志器。

    返回:
        无。

    异常:
        无。处理器只记录日志并原样返回异常响应，不吞掉异常。

    副作用:
        覆盖 FastAPI 默认的 HTTPException 处理；在 ``request.state`` 上标记
        ``exception_logged``，供请求日志 middleware 跳过重复的 ``http_request_failed``。
    """

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request.state.exception_logged = True
        extra = {
            "method": request.method,
            "path": request.url.path,
            "status_code": exc.status_code,
            "detail": exc.detail,
        }
        if exc.status_code >= 500:
            logger.error("http_exception", extra={**extra, "msg": "HTTP 异常响应"})
        else:
            logger.warning("http_exception", extra={**extra, "msg": "HTTP 异常响应"})
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )


async def _request_log_extra(request) -> dict:
    """构造 HTTP 请求日志上下文字段。

    参数:
        request: Starlette 请求对象。
        trace_id: 当前请求所属的前端操作 trace ID。

    返回:
        可传入 logging extra 的字段字典；包含 method、path、client_host、
        trace_id、业务标识以及（非 GET 请求的）query 与 JSON body 摘要。

    异常:
        无。读取 body 失败时静默跳过，不影响请求处理。

    副作用:
        读取请求路径、方法、客户端地址、query 与 JSON 请求体。
    """

    client = request.client.host if request.client is not None else ""
    path_ids = _extract_path_ids(request.url.target_directory)
    # 入口层仅绑定 trace_id（由 merge_log_context(trace_id=...) 完成，D4）；
    # 从 path 提取的业务实体 ID 收敛进 data，不再作为独立链路键。
    data: dict[str, object] = {}
    if path_ids["task_id"]:
        data["task_id"] = path_ids["task_id"]
    if path_ids["run_id"]:
        data["run_id"] = path_ids["run_id"]
    extra = {
        "method": request.method,
        "path": request.url.target_directory,
        "client_host": client,
        "data": data,
    }
    query = request.url.query
    if query:
        extra["query"] = query
    if request.method != "GET":
        body_value = await _request_body_value(request)
        if body_value is not None:
            # 解析为 dict 后写入 data.body，使脱敏能按 key 名递归遮蔽
            # api_key/token/secret 等敏感字段（redact_value 对字符串只截断不扫描）。
            data["body"] = body_value
    return extra


async def _request_body_value(request) -> object | None:
    """读取并解析 JSON 请求体，供结构化日志脱敏。

    仅处理 ``application/json`` 请求体；其他类型（表单、上传、流）不记录，
    避免消耗大文件流或二进制内容。读取或解析失败时返回 ``None``，
    不记录原始文本，防止 body 内明文 secret 绕过脱敏落盘。

    参数:
        request: Starlette 请求对象。

    返回:
        解析后的 JSON 值（通常为 dict）；无 body、读取失败或 JSON 解析失败时返回 ``None``。

    异常:
        无。任何读取或解析异常都会降级为返回 ``None``，不影响请求处理。

    副作用:
        读取并缓存请求体（Starlette 会缓存，下游仍可正常解析）。
    """

    if "application/json" not in request.headers.get("content-type", ""):
        return None
    try:
        raw = await request.body()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None


def _duration_ms(started_at: float) -> int:
    """计算从开始时间到当前时间的毫秒耗时。

    参数:
        started_at: ``time.perf_counter`` 返回的开始时间。

    返回:
        非负整数毫秒。

    异常:
        无。

    副作用:
        读取单调时钟。
    """

    return max(0, int((time.perf_counter() - started_at) * 1000))


def _request_trace_id(trace_id_header: str) -> str:
    """解析请求携带的 trace_id，缺失或非法时生成新值。

    参数:
        trace_id_header: 请求 header 中的 ``x-trace-id`` 文本。

    返回:
        合法 ``x-trace-id``；缺失或非法时返回新生成 trace id。

    异常:
        无。

    副作用:
        非法或缺失时生成新的 trace id。
    """

    clean_trace_id = trace_id_header.strip().lower()
    if is_trace_id(clean_trace_id) and clean_trace_id != "0" * 32:
        return clean_trace_id
    return new_trace_id()


def _extract_path_ids(path: str) -> dict[str, str]:
    """从常见 API 路径中提取业务标识。

    参数:
        path: 请求路径。

    返回:
        包含 task_id 和 run_id 的字典；无法解析时值为空字符串。

    异常:
        无。

    副作用:
        无。
    """

    segments = [segment for segment in path.split("/") if segment]
    values = {"task_id": "", "run_id": ""}
    for index, segment in enumerate(segments[:-1]):
        next_value = segments[index + 1]
        if segment == "tasks":
            values["task_id"] = next_value
        elif segment == "runs":
            values["run_id"] = next_value
    return values

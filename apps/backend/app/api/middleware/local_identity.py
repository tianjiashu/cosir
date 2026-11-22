"""桌面控制面的一次启动身份；不是用户账号，凭据不进入 Agent 的环境或磁盘。

未注入 ``CODING_AGENT_API_TOKEN`` 时一律拒绝（fail closed），没有「本机就放行」的旁路。
本层在校验失败处自己记日志：请求日志中间件注册得更早、位于本层内侧，看不到这里拦下的请求，
而 403 的原因正是排查时最需要的一条事实。
"""

import os
import secrets
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config.logging.logger import log

_CAPABILITY_ENV = "CODING_AGENT_API_TOKEN"
_CAPABILITY_HEADER = b"x-cosir-capability"
_TRACE_HEADER = b"x-trace-id"
# 公开路径：存活探针（宿主 readiness）与宿主工具链路（自带首帧 token，校验在路由内）。
_PUBLIC_PATHS = frozenset({"/health", "/host/link"})
# 同一调用方持续被拒时按 (类型, 路径, 是否带凭据) 限流，避免刷爆日志；被拒事实仍逐条生效。
_REJECT_LOG_INTERVAL_SECONDS = 1.0
_last_reject_logged: dict[tuple[str, str, bool], float] = {}


def warn_if_capability_missing() -> None:
    """启动期检查身份是否注入；缺失时所有领域接口都会 403，只在这里提醒一次。"""

    if os.environ.get(_CAPABILITY_ENV, ""):
        return
    log.warning(
        "local_identity_token_missing",
        extra={
            "msg": f"未注入本机 API 身份（{_CAPABILITY_ENV}），所有领域接口将返回 403",
            "data": {"env": _CAPABILITY_ENV},
        },
    )


class LocalIdentityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or scope["path"] in _PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        # OPTIONS 没有业务副作用；实际请求仍必须出示凭据。
        if scope["type"] == "http" and scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        provided = headers.get(_CAPABILITY_HEADER, b"").decode("ascii", errors="ignore")
        expected = os.environ.get(_CAPABILITY_ENV, "")
        if expected and secrets.compare_digest(provided, expected):
            await self.app(scope, receive, send)
            return
        _log_rejection(scope, headers, capability_present=bool(provided))
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
        else:
            await JSONResponse({"detail": "local process identity required"}, status_code=403)(
                scope, receive, send
            )


def _log_rejection(
    scope: Scope, headers: dict[bytes, bytes], *, capability_present: bool
) -> None:
    """记录一次被拒的请求；同一 (类型, 路径, 是否带凭据) 在限流窗口内只记一条。"""

    key = (scope["type"], scope["path"], capability_present)
    now = time.monotonic()
    if now - _last_reject_logged.get(key, 0.0) < _REJECT_LOG_INTERVAL_SECONDS:
        return
    _last_reject_logged[key] = now
    # 前端用 X-Trace-Id 记链路；这一层的拒绝发生在请求日志上下文之外，把它带进 data 才能跨端对上。
    trace_id = headers.get(_TRACE_HEADER, b"").decode("ascii", errors="ignore")[:64]
    log.warning(
        "local_identity_rejected",
        extra={
            "msg": "本机 API 身份校验未通过，请求被拒绝",
            "data": {
                "kind": scope["type"],
                "method": scope.get("method", ""),
                "path": scope["path"],
                "capability_present": capability_present,
                "token_configured": bool(os.environ.get(_CAPABILITY_ENV, "")),
                "trace_id": trace_id,
            },
        },
    )

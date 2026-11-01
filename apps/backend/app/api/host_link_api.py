"""宿主工具链路端点（WebSocket ``/host/link``）。

桌面宿主在后端就绪后主动连接；首帧 ``hello`` 携带宿主拉起后端时注入的 token（不放 URL 查询串，
避免进日志）。网页同样能连 ``127.0.0.1`` 上的 WebSocket（CORS 管不到 WebSocket），所以 token 是
这里唯一的身份依据：校验失败以 4003 关闭。之后的帧全部交给 ``HostLink``。
"""

from typing import Any

from fastapi import Depends, WebSocket, WebSocketDisconnect

from app.app import app
from app.config.logging.logger import log
from app.service.depends import get_host_link
from app.tools.host.host_link import HostLink

CLOSE_UNAUTHORIZED = 4003
CLOSE_PROTOCOL_ERROR = 1008


@app.websocket("/host/link")
async def host_link(websocket: WebSocket, link: HostLink = Depends(get_host_link)) -> None:
    await websocket.accept()
    try:
        hello = await websocket.receive_json()
        host = _parse_hello(hello)
        if not link.accepts(hello.get("token")):
            log.warning(
                "host_link_rejected",
                extra={"msg": "宿主工具链路认证失败", "data": {"host": host}},
            )
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="invalid host token")
            return
        await link.serve(websocket, host)
    except WebSocketDisconnect:
        pass
    except (ValueError, TypeError) as exc:  # json.JSONDecodeError 是 ValueError 子类
        log.warning(
            "host_link_protocol_error",
            extra={"msg": "宿主工具链路帧非法", "data": {"error": str(exc)}},
        )
        await websocket.close(code=CLOSE_PROTOCOL_ERROR, reason="host link protocol error")


def _parse_hello(frame: object) -> dict[str, Any]:
    """校验首帧形状并返回宿主自报信息（只进日志）。"""

    if not isinstance(frame, dict) or frame.get("type") != "hello":
        raise ValueError("the first host frame must be hello")
    host = frame.get("host", {})
    if not isinstance(host, dict):
        raise ValueError("hello.host must be an object")
    return host

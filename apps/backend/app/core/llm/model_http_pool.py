"""模型请求共享 HTTP 连接池。

承载进程级 ``httpx.AsyncClient`` 的复用与生命周期管理，是 LLM 调用的基础设施层
（对外 HTTP 交互资源），与具体 provider 的构造逻辑解耦。每次模型请求经 LangChain
``ChatOpenAI`` 发起，若每次 turn 都新建客户端会重复 TCP/TLS 握手（HTTPS 下通常
20~80ms）；本模块按「事件循环 + base_url」分组复用客户端，使同循环内的连续请求
复用连接。

关键约束：
- ``httpx.AsyncClient`` 与其首次使用时的运行事件循环绑定；跨循环复用会抛
  ``RuntimeError: Event loop is closed`` 等错误。因此缓存键含事件循环标识，并显式
  要求调用时存在运行循环。
- ``ChatOpenAI`` 不会自动关闭传入的客户端（无 ``__del__``），故由本模块在进程退出时
  统一 ``aclose``（见 ``close_shared_model_http_clients``），避免连接泄漏。
"""

import httpx

from app.config.logging.logger import log

# 共享客户端缓存：外层键为事件循环 id，内层键为归一后的 base_url。
# 双维度避免跨循环复用绑定到已关闭循环的客户端。
_SHARED_HTTP_CLIENTS: dict[int, dict[str, httpx.AsyncClient]] = {}

# openai 默认端点（base_url 为 None 时 ChatOpenAI 使用的地址），统一归一成缓存 key。
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1/"

# 单用户桌面场景连接数上限；非并发服务假设下取小值，避免过度预留。
_MAX_CONNECTIONS = 2


def _normalize_base_url(base_url: str | None) -> str:
    """把 base_url 归一成缓存键：None 回落到 openai 默认端点。

    参数:
        base_url: 各 provider 解析出的端点；可能为 None（沿用 openai 默认）。

    返回:
        用于客户端缓存字典的稳定 key 字符串。

    异常:
        无。

    副作用:
        无。
    """

    return base_url or _DEFAULT_OPENAI_BASE_URL


def get_shared_model_http_client(base_url: str | None) -> httpx.AsyncClient:
    """获取（惰性创建）按「事件循环 + base_url」分组的共享异步 HTTP 客户端。

    同一事件循环、同一 base_url 在进程生命周期内复用同一个 ``httpx.AsyncClient``，
    使模型请求复用 TCP/TLS 连接。客户端在当前运行事件循环内首次惰性创建
    （httpx 要求与运行循环绑定）；调用方必须在运行事件循环内调用，否则快速失败。
    连接池与 TCP keep-alive 由 httpx 默认开启，无需额外参数。

    参数:
        base_url: 端点 URL；为 None 时回落到 openai 默认端点。

    返回:
        按「事件循环 + base_url」缓存的 ``httpx.AsyncClient`` 单例。

    异常:
        RuntimeError: 当当前无可运行的事件循环时抛出（客户端无法绑定循环）。

    副作用:
        首次命中某「循环 + base_url」时在事件循环内创建并缓存一个 AsyncClient；
        不自动关闭（由 ``close_shared_model_http_clients`` 统一释放）。
    """

    import asyncio

    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    key = _normalize_base_url(base_url)
    per_loop = _SHARED_HTTP_CLIENTS.setdefault(loop_id, {})
    cached = per_loop.get(key)
    if cached is not None and not cached.is_closed:
        return cached
    # 必须在事件循环内创建（httpx 绑定运行循环）；单用户桌面场景连接数设 2 即可。
    client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_CONNECTIONS,
        ),
    )
    per_loop[key] = client
    return client


async def close_shared_model_http_clients() -> None:
    """统一关闭所有共享模型 HTTP 客户端，供应用关闭时调用。

    由 FastAPI lifespan 的 finally 分支触发，确保进程退出前释放连接，避免连接泄漏。
    按事件循环维度清理；关闭失败记录完整堆栈以便排查，不向上抛。

    参数:
        无。

    返回:
        无。

    异常:
        无（单个客户端关闭失败仅记录日志，不向上抛）。

    副作用:
        关闭并清空 ``_SHARED_HTTP_CLIENTS`` 中所有客户端。
    """

    for loop_id, per_loop in _SHARED_HTTP_CLIENTS.items():
        for base_url, client in per_loop.items():
            if client.is_closed:
                continue
            try:
                await client.aclose()
            except (httpx.HTTPError, RuntimeError):
                log.exception(
                    "llm_http_client_close_failed",
                    extra={
                        "msg": "关闭共享模型 HTTP 客户端失败",
                        "data": {"loop_id": loop_id, "base_url": base_url},
                    },
                )
    _SHARED_HTTP_CLIENTS.clear()

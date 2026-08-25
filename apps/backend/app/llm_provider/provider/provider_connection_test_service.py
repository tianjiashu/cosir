"""模型厂商连通性测试服务（用户视角三件套之一，设计文档 §七 阶段 2）。

单一职责：在用户主动触发「测试连接」时，用已配置的厂商参数
（``base_url`` / ``api_key``）发起一次最小 chat 请求，
返回成功 / 失败 + 可读错误码（经 ``model_error_mapper`` 归一，阶段 4）。
**不负责**：模型清单实际拉取（``provider_discover_service``）、模型解析链
（``model_resolver_service``）。

用户视角主线（设计文档 §三）：用户在设置面板配完厂商 + Key 后，需要即时
验证凭据 / 端点可用性——本服务是该体验的后端入口，对应
``POST /providers/{provider_id}/test`` 端点（``providers_api``）。

失败语义（设计文档 §7.1）：测试失败属「外部依赖调用」，结果值对象归一为
``ConnectionTestResult``，由 API 层转 200 携带 ``success=False`` + 错误码
（区别于 discover 的 502：连通性测试本身就是「试错」语义，失败是正常结果
之一，不抛 HTTP 异常）。
"""

from dataclasses import dataclass
from time import perf_counter

from app.config.logging.logger import log
from app.llm_provider.model_error_mapper import map_litellm_error
from app.llm_provider.provider.provider_capability import get_capability
from app.models import ProviderRecord


@dataclass(frozen=True)
class ConnectionTestResult:
    """一次连通性测试的结果值对象。

    属性:
        provider_id: 被测试的厂商标识（日志与响应回填用）。
        success: 测试是否成功（成功时 ``error_code`` / ``error_message`` 均为 None）。
        elapsed_ms: 测试耗时（毫秒），供 UI 展示「响应速度」与排查慢请求。
        error_code: 失败时的稳定错误码（与阶段 4 ``ErrorKind`` 枚举对齐，
            经 ``model_error_mapper`` 归一，如 ``model_auth_failed``）。
        error_message: 失败时面向用户的可读消息（由 mapper 提供英文引导文案）。
    """

    provider_id: str
    success: bool
    elapsed_ms: int
    error_code: str | None = None
    error_message: str | None = None


class ProviderConnectionTestService:
    """厂商连通性测试服务。"""

    def __init__(self) -> None:
        """初始化连通性测试 service。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无（阶段 2 实现时如需复用 ``ProviderService`` / ``ModelResolverService``
            单例，会经 ``app.service.depends`` 注入）。
        """

    async def test_connection(self, provider: ProviderRecord) -> ConnectionTestResult:
        """对厂商发起一次最小 chat 请求以验证凭据与端点可用性。

        实现要点：
        - 用 ``provider.base_url`` / ``api_key`` 构造一次最小
          chat 请求（``{"messages": [{"role": "user", "content": "ping"}]}``，
          ``max_tokens=1``），测试模型名取 ``capability.model_prefix + 占位模型``
          （无前缀的厂商回退 ``"ping"``，由模型客户端按
          ``api_base`` 实际打到厂商）；
        - 经 litellm ``acompletion`` 发起请求（与 ``factory.build_chat_model``
          相同的 litellm 入口），失败异常经 ``map_litellm_error`` 归一为稳定
          错误码与可读引导；
        - 成功 / 失败均写 info 级 ``provider_connection_test_*`` 日志，data 含
          ``provider_id`` / ``type`` / ``elapsed_ms`` / 失败时附 ``error_code``。

        参数:
            provider: 待测试的厂商记录（提供 base_url / api_key /
                provider_type 用于构造请求）。

        返回:
            ``ConnectionTestResult``：成功时 ``success=True``，失败时携带
            ``error_code`` 与可读 ``error_message``（``guidance`` 归一文案）。

        异常:
            无（失败语义经值对象归一返回，不向调用方抛异常；内部捕获所有
            litellm 异常转结果值对象）。

        副作用:
            发起一次到厂商端点的网络请求；写 info 级
            ``provider_connection_test_succeeded`` 或
            ``provider_connection_test_failed`` 日志。
        """
        capability = get_capability(provider.provider_type)
        # 测试模型名取 capability.model_prefix + 已知存在的最小模型占位。
        # 各厂商均有一个「通用聊天」模型（如 deepseek-chat / gpt-4o-mini），
        # 用它代替虚构的 "ping" 可避免 "The model rejected the request" 错误。
        _KNOWN_MINIMAL_MODELS: dict[str, str] = {
            "deepseek/": "deepseek-chat",
            "openai/": "gpt-4o-mini",
            "anthropic/": "claude-3-haiku-20240307",
            "gemini/": "gemini-1.5-flash",
            "azure/": "gpt-35-turbo",  # Azure 需用户自建 deployment，此为常见默认
            "dashscope/": "qwen-plus",
            "moonshot/": "moonshot-v1-8k",
            "zai/": "GLM-4-Flash",
            "volcengine/": "doubao-lite-32k",
            "tencent/": "hunyuan-lite",
            "minimax/": "abab6.5s-chat",
            "ollama/": "",  # ollama 无 Key 时跳过连通性测试
        }
        prefix = capability.model_prefix
        minimal_model = _KNOWN_MINIMAL_MODELS.get(prefix, "")
        test_model = f"{prefix}{minimal_model}" if minimal_model else f"{prefix}ping"

        start = perf_counter()
        try:
            # 与生产构建一致：显式透传 base_url / api_key 以验证
            # 用户配置，而非让 litellm 按前缀内置解析（那测不到用户自定义端点）。
            await _acompletion_ping(
                model=test_model,
                api_base=provider.base_url,
                api_key=provider.api_key,
            )
        except Exception as exc:  # 连通性测试需捕获一切外部异常以归一为结果值对象
            elapsed_ms = int((perf_counter() - start) * 1000)
            mapped = map_litellm_error(exc)
            log.info(
                "provider_connection_test_failed",
                extra={
                    "msg": (
                        f"厂商连通性测试失败：provider={provider.name}"
                        f"（type={provider.provider_type}）"
                    ),
                    "data": {
                        "provider_id": provider.id,
                        "type": provider.provider_type,
                        "elapsed_ms": elapsed_ms,
                        "error_code": mapped.error_code.value,
                        "retryable": mapped.retryable,
                    },
                },
            )
            return ConnectionTestResult(
                provider_id=provider.id,
                success=False,
                elapsed_ms=elapsed_ms,
                error_code=mapped.error_code.value,
                error_message=mapped.guidance,
            )

        elapsed_ms = int((perf_counter() - start) * 1000)
        log.info(
            "provider_connection_test_succeeded",
            extra={
                "msg": (
                    f"厂商连通性测试成功：provider={provider.name}"
                    f"（type={provider.provider_type}）"
                ),
                "data": {
                    "provider_id": provider.id,
                    "type": provider.provider_type,
                    "elapsed_ms": elapsed_ms,
                },
            },
        )
        return ConnectionTestResult(
            provider_id=provider.id,
            success=True,
            elapsed_ms=elapsed_ms,
        )


async def _acompletion_ping(
    *,
    model: str,
    api_base: str | None,
    api_key: str | None,
) -> None:
    """经 litellm 发起一次最小 chat 请求（连通性测试的内部封装）。

    参数:
        model: 测试用模型名（``capability.model_prefix + 占位模型``）。
        api_base: 厂商自定义端点（None 时不传，由 litellm 按前缀解析）。
        api_key: 厂商 Key 明文（None 时不传）。

    返回:
        无。

    异常:
        litellm 异常透传（由调用方 ``test_connection`` 归一为错误码）。

    副作用:
        发起一次到厂商端点的网络请求；不写日志（日志统一在调用方收口）。
    """
    from litellm import acompletion

    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    if api_base:
        kwargs["api_base"] = api_base
    if api_key:
        kwargs["api_key"] = api_key
    await acompletion(**kwargs)

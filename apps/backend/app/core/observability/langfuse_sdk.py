"""Langfuse SDK 适配与 payload 限制。

把锁定版本的 Langfuse v4 API 隔离在生命周期管理器之外，并提供根 Trace / OTel span 的
payload 大小限制回调。所有 Langfuse import 均为惰性加载。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from app.config.logging.logger import log
from app.core.observability.langfuse_config import LangfuseConfig
from app.core.observability.langfuse_payload_limits import limit_langfuse_payload


class LangfuseSdk(Protocol):
    """Langfuse SDK 的最小适配契约，便于生命周期逻辑与三方依赖隔离。"""

    def is_available(self) -> bool:
        """返回当前进程是否可以导入 Langfuse SDK。"""

    def build_client(self, config: LangfuseConfig) -> Any:
        """按配置创建 client。"""

    def build_callback_handler(self, public_key: str) -> Any:
        """创建绑定当前 public key 的 LangChain callback handler。"""

    def propagate_attributes(self, **kwargs: Any) -> Any:
        """创建 Langfuse 属性传播上下文。"""

    def reset_resource(self, public_key: str) -> None:
        """移除 SDK 按 public key 注册的单个进程级资源。"""


class _LangfuseSdkAdapter:
    """把锁定版本的 Langfuse v4 API 隔离在生命周期管理器之外。"""

    def is_available(self) -> bool:
        """返回 Langfuse 包是否可导入。"""

        try:
            import langfuse  # noqa: F401
        except ImportError:
            return False
        return True

    def build_client(self, config: LangfuseConfig) -> Any:
        """创建配置快照对应的 Langfuse v4 client。"""

        from langfuse import Langfuse

        return Langfuse(
            public_key=config.public_key,
            secret_key=config.secret_key,
            base_url=config.base_url,
            mask=_limit_langfuse_data,
            mask_otel_spans=_limit_langfuse_otel_spans,
        )

    def build_callback_handler(self, public_key: str) -> Any:
        """创建当前 client 对应的 LangChain callback handler。"""

        from langfuse.langchain import CallbackHandler

        return CallbackHandler(public_key=public_key)

    def propagate_attributes(self, **kwargs: Any) -> Any:
        """创建 Langfuse 根 Trace 属性传播上下文。"""

        from langfuse import propagate_attributes

        return propagate_attributes(**kwargs)

    def reset_resource(self, _public_key: str) -> None:
        """移除 Langfuse v4 按 public key 维护的单个进程级资源。

        client 的 ``shutdown`` 已由生命周期管理器负责。这里仅从 SDK 私有注册表移除当前
        public key，避免 SDK 的全局 ``reset`` 误伤其他调用方，也避免对同一个 client 重复
        flush/shutdown。该版本细节只允许存在于适配器内。
        """

        from langfuse._client.resource_manager import LangfuseResourceManager

        with LangfuseResourceManager._lock:
            LangfuseResourceManager._instances.pop(_public_key, None)


def _limit_langfuse_data(*, data: Any, **_kwargs: Any) -> Any:
    """限制 Langfuse SDK input/output payload 大小。"""

    try:
        return limit_langfuse_payload(data)
    except Exception:
        log.exception("langfuse_payload_limit_failed", extra={"msg": "Langfuse payload 限制失败"})
        return data


def _limit_langfuse_otel_spans(*, params: Any) -> Any:
    """限制 Langfuse OTel span 属性中的长文本。"""

    try:
        from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

        patches: dict[Any, Any] = {}
        for identifier, span in getattr(params, "spans", {}).items():
            replacements: dict[str, str | bool | int | float | list[str]] = {}
            for key, value in getattr(span, "attributes", {}).items():
                if isinstance(value, str):
                    limited = limit_langfuse_payload(value)
                    if isinstance(limited, str) and limited != value:
                        replacements[key] = limited
                elif isinstance(value, int | float | bool):
                    continue
                elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
                    limited_sequence = limit_langfuse_payload(list(value))
                    if limited_sequence != value and all(
                        isinstance(item, str) for item in limited_sequence
                    ):
                        replacements[key] = limited_sequence
            if replacements:
                patches[identifier] = OtelSpanPatch(set_attributes=replacements)
        return MaskOtelSpansResult(span_patches=patches) if patches else None
    except Exception:
        log.exception(
            "langfuse_otel_payload_limit_failed", extra={"msg": "Langfuse OTel payload 限制失败"}
        )
        return None

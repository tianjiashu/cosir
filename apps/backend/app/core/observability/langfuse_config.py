"""Langfuse 运行时配置与关联标识值对象。

本模块承载不可变的配置快照 ``LangfuseConfig`` 与一次 Agent Run 的关联标识 ``TraceMetadata``。
两者均为纯值对象，不触发持久化，也不依赖 Langfuse SDK。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config.settings import Settings

if TYPE_CHECKING:
    from app.core.observability.langfuse_sdk import LangfuseSdk


@dataclass(frozen=True, slots=True)
class TraceMetadata:
    """一次 Agent Run 的 Langfuse 关联标识。

    ``task_id`` 用作 Langfuse session，``run_id`` 标识本轮根 Trace，``agent_id`` 用作
    user 标识。本值对象不承载业务状态，也不触发持久化。
    """

    task_id: int
    run_id: int
    agent_id: str


@dataclass(frozen=True, slots=True)
class LangfuseConfig:
    """一次完整的 Langfuse 运行时配置快照。

    配置对象不可变，Run 获取后会一直使用同一快照。配置热更新只影响后续 Run，不会修改
    已经持有 client lease 的执行过程。
    """

    enabled: bool
    public_key: str | None
    secret_key: str | None
    base_url: str

    @classmethod
    def from_settings(cls) -> LangfuseConfig:
        """从当前 ``Settings`` 复制配置，不保留对类级可变字段的引用。"""

        return cls(
            enabled=Settings.LANGFUSE_ENABLED,
            public_key=Settings.LANGFUSE_PUBLIC_KEY,
            secret_key=Settings.LANGFUSE_SECRET_KEY,
            base_url=Settings.LANGFUSE_BASE_URL,
        )

    def fingerprint(self) -> tuple[bool, str | None, str | None, str]:
        """返回用于判断是否需要重建 client 的完整配置指纹。"""

        return (self.enabled, self.public_key, self.secret_key, self.base_url)

    def is_usable(self, sdk: LangfuseSdk) -> bool:
        """判断配置是否足以创建 Langfuse client。"""

        return bool(self.enabled and self.public_key and self.secret_key and sdk.is_available())

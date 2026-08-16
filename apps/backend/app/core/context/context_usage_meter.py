"""task 级上下文占用计量器：惰性重算 + 防抖，保证最终一致。

单一职责：基于 ``RuntimeContext.messages``（权威事实来源）与 ``TokenEstimator`` 字符估算，
计算当前上下文窗口的输入侧 token 占用；不依赖模型返回的 usage_metadata，故 turn 中途取消
也不丢数据。重算策略采用「脏标记 + 惰性重算 + 最小间隔防抖」——每次上下文变化只置脏标记
（O(1)），真正的估算在 read 时按需执行，并受最小重算间隔约束，避免高频 add_message 触发全量扫消息。

不负责：消息的采集（归 RuntimeContext）、tokenizer 词表/精确计数（按用户确认用字符估算，归
TokenEstimator）、模型配置的加载（归 ModelSettings/ModelCatalog）、上下文窗口上限的计算
（归调用方注入的 ``total_tokens_provider`` 或 ``context_window_resolver`` 单一收口）。

窗口上限语义：``total_tokens_provider`` 已统一计算 ``min(模型最大窗口, 模型覆盖窗口, 全局软上限)``，
本类不重复该规则；仅在未注入 provider 时（测试/未来扩展）回退到 ``resolve_context_window``，
复用同一套窗口解析，保证单一事实来源、无漂移。

本模块除 ``ContextUsageMeter`` 外，还提供模块级判定谓词 ``should_track_context_usage``，用于
决定某 turn 是否适用上下文占用统计（子 Agent 不统计）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from app.config.settings import Settings
from app.core.llm.context_window_resolver import resolve_context_window
from app.models.context_usage import ContextUsage
from app.utils.token_estimator import TokenEstimator


class ContextUsageMeter:
    """task 级上下文占用计量器：脏标记 + 惰性重算 + 防抖。

    线程安全：使用可重入锁保护缓存与脏标记；mark_context_changed 与 read 可并发调用。
    """

    def __init__(
        self,
        message_provider: Callable[[], list[Any]],
        model_name_provider: Callable[[], str],
        total_tokens_provider: Callable[[], int] | None = None,
    ) -> None:
        """构造计量器。

        参数:
            message_provider: 无参可调用，返回当前上下文消息实时快照（list[BaseMessage]）。
                必须返回实时引用（如 ``ctx.load_message``），不可用构造时刻的拷贝，
                否则新增消息不计入。
            model_name_provider: 无参可调用，返回当前目标模型名（如 ``deepseek-v4-flash``）。
                仅用于未注入 ``total_tokens_provider`` 时的兜底窗口解析。
            total_tokens_provider: 可选，无参可调用返回上下文窗口上限（token，已含 min 计算）；
                缺省时按 ``min(模型最大窗口, 全局软上限)`` 兜底。生产路径由 workflow 注入。

        返回:
            无。

        异常:
            无。

        副作用:
            无（仅持有可调用引用，不立即计算）。
        """
        self._messages = message_provider
        self._model_name = model_name_provider
        self._total_override = total_tokens_provider
        self._dirty = True
        self._cached: ContextUsage | None = None
        self._last_compute = 0.0
        self._lock = threading.RLock()

    def mark_context_changed(self) -> None:
        """标记上下文已变化，使下次 read 触发重算。O(1)，无 tokenize 开销。

        说明:
            高频 add_message 只置脏标记，真正的估算在 read 时按需、且受最小间隔约束执行，
            避免每个消息都全量扫 messages。
        """
        with self._lock:
            self._dirty = True

    def read(self, *, force: bool = False) -> ContextUsage:
        """返回当前占用快照；仅在脏且超过最小间隔时真正重算。

        参数:
            force: 为 True 时忽略脏标记与最小间隔，强制重算（如 model_node 在稳定上下文点读取）。

        返回:
            ContextUsage 快照（used_tokens / total_tokens）。

        异常:
            无。

        副作用:
            更新内部缓存与脏标记。
        """
        with self._lock:
            now = time.monotonic()
            within_interval = (now - self._last_compute) < Settings.CONTEXT_USAGE_MIN_INTERVAL_S
            if self._cached is not None and not force and (not self._dirty or within_interval):
                return self._cached
            self._cached = self._compute()
            self._dirty = False
            self._last_compute = now
            return self._cached

    def _total_tokens(self) -> int:
        """返回上下文窗口上限（token）。

        说明:
            优先使用注入的 ``total_tokens_provider``（调用方已完成窗口 min 计算）；
            未注入时回退到 ``resolve_context_window``，复用同一套窗口解析，保证单一事实来源。
        """
        if self._total_override is not None:
            return self._total_override()
        return resolve_context_window(self._model_name())

    def _compute(self) -> ContextUsage:
        """估算当前上下文窗口 token 占用。"""
        used = sum(self._message_tokens(m) for m in self._messages())
        return ContextUsage(used_tokens=used, total_tokens=self._total_tokens())

    @staticmethod
    def _message_tokens(message: Any) -> int:
        """提取单条消息的文本内容并估算 token 数。

        说明:
            消息可能是 HumanMessage/AIMessage/ToolMessage/SystemMessage，content 可能是 str 或
            结构化（list[dict]）。统一降级为「可读文本」再估算：str content 直接用，list content
            取 type=="text" 块的 text。无法提取时返回 0（不报错、不影响主流程）。
        """
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return TokenEstimator.estimate(content)
        if isinstance(content, list):
            text_parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return TokenEstimator.estimate("".join(text_parts))
        return 0

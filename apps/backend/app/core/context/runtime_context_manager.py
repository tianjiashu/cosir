from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from platform import system
from typing import TYPE_CHECKING, Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.context import SystemPromptBuilder
from app.core.context.context_compressor.context_compressor import ContextCompressor
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.models import RuntimeMessage
from app.utils.message_content import content_to_text

if TYPE_CHECKING:
    from app.core.context.runtime_message_store import RuntimeMessageStore


def _tool_calls_from_metadata(raw: str | None) -> list[dict[str, Any]]:
    """从 ``RuntimeMessage.metadata`` 的 JSON 字符串还原 assistant 的 tool_calls。

    与落库侧 :meth:`RuntimeContextManager._langraph_message_to_runtime_message` 的
    JSON 序列化契约对齐，供 ``AIMessage`` 重建使用。

    参数:
        raw: ``metadata.get("tool_calls")`` 的 JSON 字符串，可能为空或非法。

    返回:
        tool_calls 字典列表；空串、非法 JSON 或非列表时返回空列表。
    """

    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _default_coding_rule_dir() -> str:
    """返回默认编码规则文件的绝对路径。"""
    return str(Path(__file__).resolve().parent / "rules" / "default-coding-rules.md")


def _default_os_name() -> str:
    """惰性求值当前操作系统名称，避免类定义时固定为陈旧值。"""
    return system()


def _default_today() -> str:
    """惰性求值当天日期（ISO 格式），避免类定义时固定为陈旧值。"""
    return date.today().isoformat()


@dataclass
class RuntimeContextManager:
    """单个 task 下的运行时上下文管理器，提供 task 级隔离与上下文管理。

    职责边界：
    - **task 隔离**：每个实例绑定唯一 ``task_id``，``messages`` 与 ``lock`` 独立，
      不跨 task 共享可变状态。
    - **读写唯一入口**：内存形态（``messages``）与持久化形态（``turn_messages``）的
      转换、落库、写内存、序号维护都在本类内完成，经注入 ``store`` 端口落库
      （依赖倒置，避免 ``core/context`` 反向依赖 service）。``add_message`` 是唯一写入
      API，联合类型收口 ``BaseMessage`` / ``RuntimeMessage``，经 ``persist`` /
      ``write_memory`` 双开关正交控制落库与写内存（``write_memory=False`` 承载 turn
      启动基线的「只落库不写内存」）。
    - **变化通知**：消息变更经 :meth:`mark_context_changed` 通知已订阅 listener。
    - **压缩预留**：经可选 ``compressor`` 引用 ``ContextCompressor`` 协议与
      :meth:`maybe_compact` 暴露扩展点，暂不实现具体压缩算法。

    不负责：模型调用、工具执行、压缩算法实现；``turn_messages`` 表的删除清理
    （task/workspace 级联删除由 service 层 ``delete_by_ids`` 承担）。
    """

    # 必填：task 身份与角色画像，构造即确定，是 task 隔离的锚点。
    task_id: str
    agent_profile: AgentProfile
    # 运行时消息列表（系统提示 + 历史 + 本轮增量）。
    messages: list[RuntimeMessage] = field(default_factory=list)
    coding_rule_dir: str = field(default_factory=_default_coding_rule_dir)
    language: str = Settings.DEFAULT_LANGUAGE
    os_name: str = field(default_factory=_default_os_name)
    workspace_root: str = ""
    today: str = field(default_factory=_default_today)
    # 可重入锁：模型节点内可能嵌套调用，RLock 避免自死锁。
    lock: threading.RLock = field(default_factory=threading.RLock)
    # 最大上下文 token 数：模型配置的上下文窗口大小，用于限制消息列表长度。
    total_tokens: int = 0
    # 当前上下文消息累计占用的 token（字符估算，模型无关）。
    used_tokens: int = 0
    # 压缩预留：可选压缩器，未配置时 maybe_compact 原样返回。
    compressor: ContextCompressor | None = None
    # 消息持久化端口（依赖倒置）：由 service 层实现并注入，使 manager 成为读写唯一入口。
    # 为 None 时表示纯内存上下文（无落库能力），persist 落库请求退化为仅写内存。
    store: RuntimeMessageStore | None = None
    # 当前绑定的 turn 标识：落库（add_message persist=True）需要它定位目标 turn；
    # 为 None 时表示尚未进入某 turn，落库请求退化为仅写内存。
    current_turn_id: str | None = None
    # 轮内逐条落库序号计数器：由 manager 内部维护（替代原 RuntimeOperations 内部计数），
    # reset_message_sequence 归零、add_message 落库时自增。
    _message_sequence: int = field(default=0, init=False)
    # 上下文变化订阅者列表：按 order 排序，按需插入。
    _listeners: list[ContextListener] = field(default_factory=list, init=False)

    def add_change_listener(self, listener: ContextListener) -> RuntimeContextManager:
        """追加上下文变化订阅者，并按 ``order`` 排序。

        订阅者经 :meth:`mark_context_changed` 在消息变化时收到通知。守卫语义：
        当 ``listener.subAgent_need`` 为 ``False`` 且当前为**主 agent** 时直接跳过，
        即「不需要子 agent 订阅」的监听器只挂到子 task 上。

        参数:
            listener: 实现 ``ContextListener`` 协议的订阅者实例。

        返回:
            self（供链式调用）。
        """
        # 不需要子 agent 订阅的监听器，在主 agent 下跳过。
        if not listener.subAgent_need and self.agent_profile.main_agent:
            return self
        self._listeners.append(listener)
        self._listeners = sorted(self._listeners, key=lambda x: x.order)
        return self

    def mark_context_changed(self, event_type: ContextEventType, messages: list[RuntimeMessage]) -> None:
        """标记上下文变化，按 ``order`` 通知所有订阅者并聚合占用结果。

        把本次占用 ``used_tokens`` 与窗口上限 ``total_tokens`` 打包进 :class:`ListenerEvent`
        分发给各订阅者，再用回写的 ``result.usage`` 更新 ``used_tokens``；压缩事件时
        以订阅者回写的 ``messages_after_compressor`` 替换 ``messages``。

        参数:
            event_type: 变化来源（add / load_history / compress）。
            messages: 本次变化涉及的消息（可为本批新增，也可为完整列表）。

        返回:
            无。
        """
        listener_order_list = sorted(self._listeners, key=lambda x: x.order)
        result = ListenerResult(self.used_tokens)
        for listener in listener_order_list:
            listener.listen(ListenerEvent(event_type, messages, self.used_tokens, self.total_tokens), result)
        self.used_tokens = result.usage

        if event_type == ContextEventType.CONTEXT_COMPRESSED:
            self.messages = result.messages_after_compressor


    def __post_init__(self) -> None:
        """构造后初始化非字段状态（预置系统提示并归零序号）。

        采用 ``__post_init__`` 而非自定义 ``__init__``，确保所有 dataclass 字段由框架
        自动初始化，避免漏字段导致的 ``AttributeError``。

        副作用:
            ``messages`` 首条非 ``SystemMessage`` 时在最前插入系统提示；归零消息序号。
        """
        # 已预置系统提示时（如测试手工构造）不重复插入。
        if not self.messages or not isinstance(self.messages[0], SystemMessage):
            self.messages.insert(0, self._build_system_message())
        self._reset_message_sequence()

    def load_history(self) -> None:
        """从注入 store 读回 task 跨轮历史并追加进 ``messages``。

        经 ``store.build_for_task`` 读回该 task 的 ``RuntimeMessage`` 列表直接追加，
        并触发一次 ``LOAD_HISTORY`` 通知。无 ``store``（纯内存构造）时为空操作。

        参数:
            无。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: ``store.build_for_task`` 读取失败时透传。

        副作用:
            向 ``messages`` 追加历史消息；持锁调用 :meth:`mark_context_changed`。
        """
        if self.store is None:
            return
        history = self.store.build_for_task(self.task_id)
        with self.lock:
            self.messages.extend(history)
            self.mark_context_changed(ContextEventType.LOAD_HISTORY, copy.deepcopy(history))

    def maybe_compact(self) -> bool:
        """在压缩器已配置时压缩上下文，否则原样返回（压缩预留扩展点）。

        未配置 ``compressor`` 时直接返回 ``False``。

        返回:
            是否实际执行了压缩（``True`` 已压缩 / ``False`` 无压缩器）。

        副作用:
            压缩器配置时持锁替换 ``messages`` 为压缩结果并触发 ``CONTEXT_COMPRESSED`` 通知。
        """
        if self.compressor is None:
            return False
        with self.lock:
            self.messages = self.compressor.compact(self.messages)
            self.mark_context_changed(ContextEventType.CONTEXT_COMPRESSED, copy.deepcopy(self.messages))
        return True

    def _build_system_message(self) -> RuntimeMessage:
        """构建系统提示消息。

        返回:
            含 agent 系统提示的 ``RuntimeMessage``。
        """
        system_prompt = SystemPromptBuilder.build(self.agent_profile, self.workspace_root)
        return self._langraph_message_to_runtime_message(SystemMessage(content=system_prompt))

    def _batch_convert_langraph_messages(
            self, message_list: list[RuntimeMessage]
    ) -> list[BaseMessage]:
        """将运行时消息列表转换为 langchain 消息列表。

        逐条委托 :meth:`_runtime_message_to_langraph_message` 转换并跳过其返回 ``None``
        的消息（未知 role 的跳过语义）。

        参数:
            message_list: 待转换的运行时消息列表。

        返回:
            转换后的 langchain 消息列表。
        """
        converted: list[BaseMessage] = []
        for message in message_list:
            langchain_message = self._runtime_message_to_langraph_message(message)
            if langchain_message is not None:
                converted.append(langchain_message)
        return converted

    def load_message(self) -> list[BaseMessage]:
        """线程安全地读取当前全部消息的 langchain 形态拷贝。

        返回列表拷贝而非内部引用，避免调用方在锁外修改内部状态；逐条经
        :meth:`_runtime_message_to_langraph_message` 转换，未知 role 被跳过。

        返回:
            当前消息转换后的独立列表。
        """
        with self.lock:
            return self._batch_convert_langraph_messages(self.messages)

    def add_message(
            self,
            message: BaseMessage | RuntimeMessage,
            *,
            persist: bool = True,
            write_memory: bool = True,
    ) -> None:
        """线程安全地向上下文追加一条消息（唯一写入入口），按 ``persist`` / ``write_memory`` 落库与写内存。

        两种输入形态统一收口：``BaseMessage``（模型节点产出）落库前经
        :meth:`_langraph_message_to_runtime_message` 转换（assistant 消息先经
        :meth:`_sanitize_assistant_messages` 清洗，再 tool_calls→JSON）；``RuntimeMessage``
        （工具观察等已序列化消息）直接使用。``persist`` 与 ``write_memory`` 独立正交：
        ``persist=True, write_memory=False`` 承载 turn 启动基线「只落库不写内存」，
        ``persist=False, write_memory=True`` 仅写内存（无需重放的运行时提示）。

        ``persist=True`` 采用「先落库、成功后写内存」防撕裂：落库失败抛
        ``SQLAlchemyError`` 且内存不写；落库成功才写内存并自增序号。未注入
        ``store`` / ``current_turn_id``（纯内存构造）时 ``persist=True`` 退化为仅写内存。

        参数:
            message: 待追加的消息（``BaseMessage`` 或 ``RuntimeMessage``）。
            persist: 是否落库（默认 True）；False 时跳过落库。
            write_memory: 是否写内存（默认 True）；False 时仅落库。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: ``persist=True`` 且已注入 store/current_turn_id
                时落库失败抛出，此时内存不写（防撕裂）。

        副作用:
            ``write_memory=True`` 时向 ``messages`` 追加消息并触发 ``ADD_MESSAGE`` 通知；
            ``persist=True`` 且有落库能力时向 ``turn_messages`` 表写一行并自增序号。
        """
        if isinstance(message, RuntimeMessage):
            runtime_message = message
        else:
            runtime_message = self._langraph_message_to_runtime_message(message)

        if persist and self.store is not None and self.current_turn_id is not None:
            self.store.append(self.current_turn_id, runtime_message, self._message_sequence)
            self._message_sequence += 1

        if write_memory and runtime_message is not None:
            with self.lock:
                self.messages.append(runtime_message)
                self.mark_context_changed(ContextEventType.ADD_MESSAGE, [copy.deepcopy(runtime_message)])

    def _reset_message_sequence(self) -> None:
        """清空当前 turn 在 ``turn_messages`` 表的残留并归零序号（turn 开始执行时调用）。

        经注入 ``store`` 清空当前 ``turn_id`` 的全部行并复位序号，之后每条消息经
        :meth:`add_message` 自增落库；历史 turn 因按 ``turn_id`` 隔离不受影响。无
        ``store`` / 无 ``current_turn_id`` 时仅归零序号（纯内存）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 清理失败时抛出（由底层 CRUD 透传）。

        副作用:
            删除当前 turn 在 ``turn_messages`` 表的全部行；``_message_sequence`` 归零。
        """
        if self.store is not None and self.current_turn_id is not None:
            self.store.clear(self.current_turn_id)
        self._message_sequence = 0

    def _langraph_message_to_runtime_message(self, message: BaseMessage) -> RuntimeMessage:
        """将 langchain ``BaseMessage`` 转为内部 ``RuntimeMessage``（落库前转换收口）。

        ``AIMessage`` 的 ``tool_calls`` 以 **JSON 字符串** 存进 ``metadata["tool_calls"]``，
        与 :func:`_tool_calls_from_metadata` 的反序列化契约严格对齐；无工具调用时不写入
        该键。其余类型按 role 映射：``tool`` / ``system`` 直接转换，其它任何类型
        （含 ``user`` 与未知类型）兜底为 ``role="user"``。

        参数:
            message: 待落库的 langchain ``BaseMessage``。

        返回:
            与模型无关的 ``RuntimeMessage``，供 ``store.append`` 落库。
        """
        # assistant 消息统一经本地清洗收口（content 空串占位、丢弃 invalid_tool_calls/
        # response_metadata 等脏字段），避免脏字段回灌下一轮。
        message = self._sanitize_assistant_messages([message])[0]
        if isinstance(message, AIMessage):
            tool_calls = [
                {"name": call.get("name"), "args": call.get("args", {}), "id": call.get("id")}
                for call in (message.tool_calls or [])
            ]
            metadata: dict[str, Any] = (
                {"tool_calls": json.dumps(tool_calls, ensure_ascii=False)} if tool_calls else {}
            )
            return RuntimeMessage(
                role="assistant",
                content_text=content_to_text(message.content),
                metadata=metadata,
            )
        if isinstance(message, ToolMessage):
            return RuntimeMessage(
                role="tool",
                content_text=content_to_text(message.content),
                metadata={"tool_call_id": message.tool_call_id or ""},
            )
        if isinstance(message, SystemMessage):
            return RuntimeMessage(role="system", content_text=content_to_text(message.content))
        return RuntimeMessage(role="user", content_text=content_to_text(message.content))

    def _runtime_message_to_langraph_message(self, message: RuntimeMessage) -> BaseMessage | None:
        """将单条运行时消息转换为 langchain ``BaseMessage``（正向单条转换收口）。

        是 :meth:`_langraph_message_to_runtime_message` 的逆转换：user / assistant / tool
        / system 四种 role 分别映射 ``HumanMessage`` / ``AIMessage`` / ``ToolMessage`` /
        ``SystemMessage``；assistant 的 ``metadata["tool_calls"]`` JSON 字符串经
        :func:`_tool_calls_from_metadata` 反序列化回 langchain ``tool_calls``。仅未知 role
        返回 ``None``（跳过语义），由 :meth:`_batch_convert_langraph_messages` 跳过。

        参数:
            message: 待转换的 ``RuntimeMessage``。

        返回:
            转换后的 ``BaseMessage``；仅未知 role 返回 ``None``。
        """
        content_text = message.content_text if message.content_text is not None else ""
        if message.role == "user":
            return HumanMessage(content=content_text)
        if message.role == "assistant":
            tool_calls_meta = _tool_calls_from_metadata(message.metadata.get("tool_calls"))
            langchain_tool_calls = [
                {
                    "name": call["name"],
                    "args": call.get("args") if isinstance(call.get("args"), dict) else {},
                    "id": call.get("id") or "",
                }
                for call in tool_calls_meta
            ]
            return AIMessage(content=content_text, tool_calls=langchain_tool_calls)
        if message.role == "tool":
            return ToolMessage(
                content=content_text,
                tool_call_id=message.metadata.get("tool_call_id", ""),
            )
        if message.role == "system":
            return SystemMessage(content=content_text)
        return None

    def _sanitize_assistant_messages(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """在消息进入上下文前对 assistant 消息做最终清洗，避免脏字段回灌下一轮。

        重建后只保留安全的 ``content`` + 合法 ``tool_calls`` + ``id``；``ToolMessage``
        及其他角色不受影响（其 content=null 协议允许），保持原对象引用。

        参数:
            messages: 即将进入上下文的 LangChain 消息列表。

        返回:
            清洗后的新列表；非 assistant 类消息保持原对象引用不变。

        异常:
            无。

        副作用:
            无（不修改入参对象；仅在需要清洗的 assistant 消息时新建对象）。
        """
        normalized: list[BaseMessage] = []
        for message in messages:
            if not isinstance(message, AIMessage | AIMessageChunk):
                normalized.append(message)
                continue

            content = message.content
            # 非 str 或空串一律用单空格占位符兜底：DeepSeek 等 OpenAI 兼容端点在 assistant
            # 消息带 tool_calls 但 content 为空串时，litellm 会把 content="" 改写为 null，
            # 而 DeepSeek 拒绝 assistant.content 为 null（仅 tool 角色允许）。用非空字符串
            # 保证序列化通过，根除整类协议拒绝问题。
            if not isinstance(content, str) or content.strip() == "":
                content = " "

            # 重建时不传 invalid_tool_calls / response_metadata（当轮解析噪声与本地元数据）。
            normalized.append(
                AIMessage(
                    content=content,
                    tool_calls=message.tool_calls,
                    id=message.id,
                )
            )
        return normalized

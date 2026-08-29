from __future__ import annotations

import copy
import json
import threading
import weakref
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from platform import system
from typing import TYPE_CHECKING, Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.context import SystemPromptBuilder
from app.core.context.context_compressor.context_compressor import ContextCompressor
from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_compress_listener import ContextCompressListener
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.context_usage_compute_listener import (
    ContextUsageComputeListener,
)
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.llm_provider.provider.capability_service import CapabilityService
from app.models import RuntimeMessage, TaskRecord, TurnRecord, WorkspaceRecord
from app.service.depends import get_task_service
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


def _update_task_context_usage(task_id: int, used: int) -> None:
    """回写 task 最近一次上下文已用 token，失败降级为 error 日志不阻断主流程。

    这是 ``ensure_get_runtime_context_manager`` 挂载的**默认**回写实现。上下文占用
    是旁路统计（仅供前端占用圆环展示），其失败不应阻断上下文主流程——否则 task
    表不可用（如 storage 未初始化、DB 瞬时故障）时连 ``RuntimeContextManager``
    都无法创建，整个 turn 会直接失败。

    容错放在本默认实现而非 listener 内部：listener 是通用机制，不应替调用方决定
    容错策略；显式注入 ``update_context_usage`` 的调用方（含单测）仍按自身契约
    决定是否让失败传播。

    参数:
        task_id: 目标 task 标识。
        used: 上下文占用 token 数。

    返回:
        无（丢弃 service 返回值）。

    异常:
        无（捕获所有 ``Exception`` 并降级为 error 日志）。

    副作用:
        成功时经 task_service 回写 task 表；失败时写一条
        ``context_usage_task_update_failed`` error 日志（含堆栈，保证可排查）。
    """

    try:
        get_task_service().update_context_usage(task_id, used)
    except Exception as exc:  # 旁路统计：任何失败都只降级不阻断主流程
        log.error(
            "context_usage_task_update_failed",
            extra={
                "msg": "context usage task write-back failed; degrading to log only",
                "data": {
                    "task_id": task_id,
                    "used_tokens": used,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            },
            exc_info=True,
        )


# 弱值字典：值为 manager 实例，当外部不再持有其实例强引用时由 GC 自动回收，
# 根除「只增不删」的进程级内存泄漏，且无需在 task 删除路径手动 clear。
_runtime_context_managers: weakref.WeakValueDictionary[int, Any] = weakref.WeakValueDictionary()
task_runtime_context_managers_lock = threading.Lock()


@dataclass
class RuntimeContextManager:
    """单个 task 下的运行时上下文管理器，提供 task 级隔离与上下文管理。

    职责边界：
    - **task 隔离**：每个实例绑定唯一 ``task_id``，entries 与 ``lock`` 独立，
      不跨 task 共享可变状态。
    - **读写唯一入口**：entries 内存形态与持久化形态（``turn_messages``）的转换、
      落库、序号维护都在本类内完成，经注入 ``store`` 端口落库
      （依赖倒置，避免 ``core/context`` 反向依赖 service）。``add_message`` 是唯一写入
      API，联合类型收口 ``BaseMessage`` / ``RuntimeMessage``，经 ``persist`` 与
      ``include_in_context`` 两个正交维度分别控制落库和是否进入模型上下文。
    - **变化通知**：条目变更经 :meth:`mark_context_changed` 通知已订阅 listener，
      事件载体是 :class:`ContextEntry`（含 turn 归属），而非裸消息列表。
    - **压缩预留**：经可选 ``compressor`` 引用 ``ContextCompressor`` 协议与
      :meth:`maybe_compact` 暴露扩展点，暂不实现具体压缩算法。

    不负责：模型调用、工具执行、压缩算法实现；``turn_messages`` 表的删除清理
    （task/workspace 级联删除由 service 层 ``delete_by_ids`` 承担）。
    """

    # 必填：task 身份与角色画像，构造即确定，是 task 隔离的锚点。
    task_id: int
    agent_profile: AgentProfile
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
    # 为 None 时表示纯内存上下文（无落库能力），落库请求退化为仅写内存。
    store: RuntimeMessageStore | None = None
    # 当前绑定的 turn 标识：落库（add_message 固有契约）需要它定位目标 turn；
    # 为 None 时表示尚未进入某 turn，落库请求退化为仅写内存。
    current_turn_id: int | None = None
    # task 级 system prompt、历史 turn 和当前 turn 增量分别维护，避免通过一份列表推断归属。
    _system_entry: ContextEntry | None = field(default=None, init=False)
    _history_entries: list[ContextEntry] = field(default_factory=list, init=False)
    _active_entries: list[ContextEntry] = field(default_factory=list, init=False)
    _history_loaded: bool = field(default=False, init=False)
    # 轮内逐条落库序号计数器：由 manager 内部维护（替代原 RuntimeOperations 内部计数），
    # reset_message_sequence 归零、add_message 落库时自增。
    _message_sequence: int = field(default=0, init=False)
    # 上下文变化订阅者列表：按 order 排序，按需插入。
    _listeners: list[ContextListener] = field(default_factory=list, init=False)
    # 当前思考通道
    _thinking_channel: str = field(default="reasoning_content", init=False)

    @staticmethod
    def ensure_get_runtime_context_manager(
        agent_profile: AgentProfile,
        write_event: Callable[[Any, Any], None],
        current_workspace: WorkspaceRecord,
        current_task: TaskRecord,
        store: RuntimeMessageStore,
        turn: TurnRecord,
    ) -> RuntimeContextManager:
        """获取或创建指定 task_id 的运行时上下文管理器。

        仅负责「获取或创建」task 级配置（agent_profile / listeners / 已加载的
        task 历史 / workspace_root），不绑定 turn 执行态。turn 级执行态
        （current_turn_id / total_tokens / write_event）由调用方在拿到实例后
        显式调用 ``bind_turn`` 绑定，时序清晰、职责单一。
        """
        task_id = current_task.id
        if task_id in _runtime_context_managers:
            return _runtime_context_managers[task_id]
        with task_runtime_context_managers_lock:
            if task_id in _runtime_context_managers:
                return _runtime_context_managers[task_id]

            # 创建上下文管理器
            runtime_context_manager = (
                RuntimeContextManager(
                    agent_profile=agent_profile,
                    workspace_root=current_workspace.root_path,
                    task_id=task_id,
                    store=store,
                    current_turn_id=turn.id,
                    total_tokens=CapabilityService.get_model_context_window(turn.model_name),  # 300K
                )
                .add_change_listener(
                    ContextUsageComputeListener(
                        write_event=write_event,
                        update_context_usage=_update_task_context_usage,
                        task_id=task_id,
                    )
                )
                .add_change_listener(ContextCompressListener())
            )

            # 加载 task 历史消息
            runtime_context_manager.load_history(excluded_turn_ids={turn.id})
            _runtime_context_managers[task_id] = runtime_context_manager
            log.info(
                "runtime_context_manager created",
                extra={
                    "task_id": task_id,
                    "turn_id": turn.id,
                    "registered_managers": len(_runtime_context_managers),
                },
            )
            return runtime_context_manager

    def begin_turn(
        self,
        turn: TurnRecord,
        mode: Literal["fresh", "resume"] = "fresh",
    ) -> None:
        """绑定一个 turn，并按 fresh/resume 语义初始化当前 turn 增量。

        参数:
            turn: 待绑定的 turn 记录。
            mode: ``fresh`` 表示新执行/重跑并清空该 turn；``resume`` 表示从持久化轨迹恢复。

        返回:
            无。

        异常:
            ValueError: ``mode`` 不是 ``fresh`` 或 ``resume`` 时抛出。
            ValueError: turn 不属于当前 task 时抛出。
            sqlalchemy.exc.SQLAlchemyError: 清理或读取 turn 消息失败时抛出。

        副作用:
            更新当前 turn、上下文窗口和 active entries；fresh 模式清理当前 turn 持久化轨迹。
        """
        if mode not in {"fresh", "resume"}:
            raise ValueError(f"unsupported context execution mode: {mode}")
        if turn.task_id != self.task_id:
            raise ValueError(
                f"turn {turn.id} belongs to task {turn.task_id}, expected {self.task_id}"
            )
        with self.lock:
            # 保存当前 turn 的 active entries 到历史记录
            previous_turn_id = self.current_turn_id

            # 存在turn重放的情况，如果turn一致，则说明是重放，则不追加到历史记录中
            if previous_turn_id != turn.id and self._active_entries:
                self._history_entries.extend(self._active_entries)
            self._history_entries = [
                entry for entry in self._history_entries if entry.turn_id != turn.id
            ]
            # 更新当前 turn 状态
            self.current_turn_id = turn.id
            # 更新上下文窗口
            self.total_tokens = CapabilityService.get_model_context_window(turn.model_name or "")
            if mode == "fresh":
                # 清空当前 turn 的 active entries：重放场景
                self._active_entries = []
                self._reset_message_sequence()
            else:
                # 从持久化轨迹恢复 active entries：恢复场景
                restored_entries = (
                    self.store.build_for_turn(turn.id) if self.store is not None else []
                )
                self._active_entries = list(restored_entries)

                self._message_sequence = self.store.next_sequence(turn.id)
            self.mark_context_changed(
                ContextEventType.LOAD_HISTORY,
                self._effective_entries(),
                allow_write_event_failure=True,
            )
            log.info(
                "runtime_context_manager rebound to turn",
                extra={
                    "task_id": self.task_id,
                    "previous_turn_id": previous_turn_id,
                    "turn_id": turn.id,
                    "mode": mode,
                },
            )

    def upsert_current_user_message(
        self,
        message: RuntimeMessage,
        *,
        allow_write_event_failure: bool = False,
    ) -> None:
        """写入或刷新当前 turn 的 user 消息，不重复追加持久化轨迹。

        resume 场景从数据库恢复的 user 消息没有运行期多模态 block；本方法在已有 user
        entry 上只刷新内存表示，缺失时才经 :meth:`add_message` 持久化并追加。

        参数:
            message: 当前 turn 的 user 消息。
            allow_write_event_failure: 事件 writer 尚未建立时是否允许继续；workflow
                在 graph 启动前写入 turn 基线时应传 True。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 缺少 user entry 且追加持久化失败时抛出。

        副作用:
            更新当前 turn 的 user entry；必要时追加一条持久化消息并触发 usage 更新。
        """
        with self.lock:
            for index, entry in enumerate(self._active_entries):
                if entry.message.role != "user":
                    continue
                self._active_entries[index] = ContextEntry(
                    message=message,
                    turn_id=self.current_turn_id,
                )
                self.mark_context_changed(
                    ContextEventType.ADD_MESSAGE,
                    self._effective_entries(),
                    allow_write_event_failure=allow_write_event_failure,
                )
                return
        self.add_message(
            message,
            include_in_context=True,
            allow_write_event_failure=allow_write_event_failure,
        )

    def set_thinking_channel(self, channel: str) -> None:
        self._thinking_channel = channel

    def add_change_listener(self, listener: ContextListener) -> RuntimeContextManager:
        """追加上下文变化订阅者，并按 ``order`` 排序。

        订阅者经 :meth:`mark_context_changed` 在消息变化时收到通知。守卫语义：
        当 ``listener.main_agent_only`` 为 ``True`` 且当前为**子 agent** 时直接跳过，
        即主 Agent 专属监听器不会挂到委派子 task 上。

        参数:
            listener: 实现 ``ContextListener`` 协议的订阅者实例。

        返回:
            self（供链式调用）。
        """
        # 主 Agent 专属监听器，在子 Agent 下跳过。
        if listener.main_agent_only and not self.agent_profile.main_agent:
            return self
        self._listeners.append(listener)
        self._listeners = sorted(self._listeners, key=lambda x: x.order)
        return self

    def mark_context_changed(
        self,
        event_type: ContextEventType,
        entries: list[ContextEntry],
        *,
        allow_write_event_failure: bool = False,
    ) -> None:
        """标记上下文变化，按 ``order`` 通知所有订阅者并聚合占用结果。

        入参是**上下文条目**而非裸消息：条目除消息外还携带 ``turn_id`` 归属，压缩等
        需要按 turn 切分/保留上下文的 listener 才能工作；只需消息的实现自行从
        ``entry.message`` 派生，事件不再同时维护两份等价快照。

        快照隔离由本方法统一收口：``entries`` 经深拷贝后分发，listener 对快照的任何
        修改（含 ``entry.message.metadata`` 这类可变子对象）都不会回灌 manager 内部
        状态，调用方无需自行拷贝。

        把本次占用 ``used_tokens`` 与窗口上限 ``total_tokens`` 打包进 :class:`ListenerEvent`
        分发给各订阅者，再用回写的 ``result.usage`` 更新 ``used_tokens``。上下文状态只
        由 entries 管理，listener 不得反向替换上下文。

        参数:
            event_type: 变化来源（add / load_history / compress）。
            entries: 变更后的有效上下文条目完整快照。全部调用点均传
                :meth:`_effective_entries`（系统 + 历史 + 当前 turn 全量），不传增量批次。
            allow_write_event_failure: 是否允许事件写入器不可用时继续执行。

        返回:
            无。

        异常:
            无（捕获之外的失败由 listener 自身抛出并原样传播）。

        副作用:
            通知所有 listener，并用 ``result.usage`` 覆盖 ``used_tokens``。
        """
        listener_order_list = sorted(self._listeners, key=lambda x: x.order)
        result = ListenerResult(self.used_tokens)
        snapshot = copy.deepcopy(entries)
        try:
            for listener in listener_order_list:
                listener.listen(
                    ListenerEvent(
                        event_type,
                        snapshot,
                        self.used_tokens,
                        self.total_tokens,
                        allow_write_event_failure=allow_write_event_failure,
                    ),
                    result,
                )
        finally:
            self.used_tokens = result.usage

    def __post_init__(self) -> None:
        """构造 task 级 system entry 并初始化轮内序号。

        返回:
            无。

        异常:
            无。

        副作用:
            创建当前 task 的 system prompt entry。
        """
        self._system_entry = ContextEntry(self._build_system_message(), None)

    def load_history(
        self,
        excluded_turn_ids: Collection[int] | None = None,
        *,
        replace: bool = True,
    ) -> None:
        """从注入 store 读回历史并替换 task 级 history entries。

        ``excluded_turn_ids`` 用于排除当前正在执行的 turn，使当前 turn 由后续
        ``add_message`` 作为运行期增量写入，避免恢复/重跑时把旧轨迹带入上下文。
        经 ``store.build_for_task`` 读回消息后默认替换历史区，当前 active entries 不变，
        因而重复调用不会累加；``replace=False`` 仅用于明确的追加式调用。随后触发
        一次 ``LOAD_HISTORY`` 通知。无 ``store``（纯内存构造）时为空操作。

        参数:
            excluded_turn_ids: 需要排除的 turn 标识集合，可选。
            replace: 是否替换已有历史；默认 True，避免重复加载。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: ``store.build_for_task`` 读取失败时透传。

        副作用:
            替换 history entries 并通知上下文变化；持锁调用 :meth:`mark_context_changed`。
        """
        if self.store is None:
            return
        with self.lock:
            effective_excluded_turn_ids = set(excluded_turn_ids or ())
            if self.current_turn_id is not None:
                effective_excluded_turn_ids.add(self.current_turn_id)
            history = self.store.build_for_task(self.task_id, effective_excluded_turn_ids)
            entries = list(history)
            if replace:
                self._history_entries = entries
            else:
                self._history_entries.extend(entries)
            self._history_loaded = True
            self.mark_context_changed(
                ContextEventType.LOAD_HISTORY,
                self._effective_entries(),
                allow_write_event_failure=True,
            )

    def _effective_entries(self) -> list[ContextEntry]:
        """返回当前进入模型上下文的条目。

        内存中只持有进模型的上下文（``in_context`` 为假的消息不进入内存，仅在持久化层
        ``turn_messages`` 以 ``in_context`` 列标记），因此此处无需再按该标记过滤，直接
        拼接系统 / 历史 / 活跃三段即可。

        并发契约：调用方必须已持有 ``self.lock``。本方法为纯读（仅读取并拼接
        ``_system_entry`` / ``_history_entries`` / ``_active_entries``，不修改状态），不在
        内部重复加锁；锁边界由调用方负责，所有公开入口（``load_message`` / ``maybe_compact``
        等）及经 ``mark_context_changed`` 的内部调用均已持锁，禁止在锁外直接读取。
        """
        entries: list[ContextEntry] = []
        if self._system_entry is not None:
            entries.append(self._system_entry)
        entries.extend(self._history_entries)
        entries.extend(self._active_entries)
        return entries

    def _effective_runtime_messages(self) -> list[RuntimeMessage]:
        """返回当前有效上下文的消息投影（从 :meth:`_effective_entries` 派生）。

        仅供模型读取出口 :meth:`load_message` 使用；变化通知通道传条目本身
        （见 :meth:`mark_context_changed`），不走本投影，以免 listener 丢失 turn 归属。

        并发契约：调用方必须已持有 ``self.lock``（与 :meth:`_effective_entries` 一致）。

        返回:
            当前有效上下文的消息列表（新列表，元素与条目共享 ``RuntimeMessage`` 引用）。
        """
        return [entry.message for entry in self._effective_entries()]

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
            return self._batch_convert_langraph_messages(self._effective_runtime_messages())

    def add_message(
        self,
        message: BaseMessage | RuntimeMessage,
        *,
        include_in_context: bool = True,
        allow_write_event_failure: bool = False,
    ) -> None:
        """线程安全地向上下文追加消息，按持久化与上下文归属分别处理。

        两种输入形态统一收口：``BaseMessage``（模型节点产出）落库前经
        :meth:`_langraph_message_to_runtime_message` 转换（assistant 消息先经
        :meth:`_sanitize_assistant_messages` 清洗，再 tool_calls→JSON）；``RuntimeMessage``
        （工具观察等已序列化消息）直接使用。

        内存与持久化分层：内层持有「进模型的上下文」，只有 ``include_in_context=True``
        的消息才进入内存 active entries；持久化层 ``turn_messages`` 始终完整保存每条消息，
         并以 ``in_context`` 列标记其是否进模型，供审计 / 回放读取完整历史。

        持久化是 :meth:`add_message` 的固有契约：只要注入 ``store`` 且已绑定
        ``current_turn_id``，每条消息都落库（含 ``include_in_context=False`` 的 deferred
        修复提示），保证审计轨迹完整。落库采用「先落库、成功后写内存」防撕裂：落库失败
        抛 ``SQLAlchemyError`` 且内存不写；落库成功才按 ``include_in_context`` 决定是否写
        内存并自增序号。未注入 ``store`` / ``current_turn_id``（纯内存构造）时退化为仅写
        内存，便于无落库能力的单元测试。

        参数:
            message: 待追加的消息（``BaseMessage`` 或 ``RuntimeMessage``）。
            include_in_context: 是否进入当前模型上下文（默认 True）；False 时仅落库轨迹、
                不进入内存上下文。
            allow_write_event_failure: 事件 writer 尚未建立时是否允许继续。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 已注入 store/current_turn_id 时落库失败抛出，
                此时内存不写（防撕裂）。

        副作用:
            ``include_in_context=True`` 时向 active entries 追加消息并触发 ``ADD_MESSAGE`` 通知；
            有落库能力时向 ``turn_messages`` 表写一行（含 ``in_context`` 标记）并自增序号。
        """
        if isinstance(message, RuntimeMessage):
            runtime_message = message
        else:
            runtime_message = self._langraph_message_to_runtime_message(message)

        if self.store is not None and self.current_turn_id is not None:
            self.store.append(
                self.current_turn_id,
                runtime_message,
                self._message_sequence,
                include_in_context=include_in_context,
            )
            self._message_sequence += 1

        if include_in_context and runtime_message is not None:
            with self.lock:
                self._active_entries.append(
                    ContextEntry(
                        message=runtime_message,
                        turn_id=self.current_turn_id,
                    )
                )
                self.mark_context_changed(
                    ContextEventType.ADD_MESSAGE,
                    self._effective_entries(),
                    allow_write_event_failure=allow_write_event_failure,
                )

    def _reset_message_sequence(self) -> None:
        """清空当前 turn 在 ``turn_messages`` 表的残留并归零序号。

        经注入 ``store`` 清空当前 ``turn_id`` 的全部行并复位序号，之后每条消息经
        :meth:`add_message` 自增落库；历史 turn 因按 ``turn_id`` 隔离不受影响。无
        ``store`` / 无 ``current_turn_id`` 时仅归零序号（纯内存）。

        仅由 :meth:`begin_turn` 在 ``fresh`` 模式清空当前 turn 时调用：保证新 turn 序号从
        0 计，且同 turn_id resume 重试时清空上一轮中途异常残留，落库幂等。

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
            metadata["reasoning_content"] = message.additional_kwargs.get(self._thinking_channel)
            return RuntimeMessage(
                role="assistant",
                content_text=content_to_text(message.content),
                metadata=metadata,
            )
        if isinstance(message, ToolMessage):
            metadata = {
                "tool_call_id": message.tool_call_id or "",
                "reasoning_content": message.additional_kwargs.get(self._thinking_channel),
            }
            return RuntimeMessage(
                role="tool",
                content_text=content_to_text(message.content),
                metadata=metadata,
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
            # 多模态：运行期内存态 content_blocks（image_url 等）优先透传为 HumanMessage
            # 的 list[dict] content；无 block 时退回纯文本。content_blocks 不落库，仅当前轮由
            # workflow 经 vision_content_blocks 注入，历史轮回放保持纯文本。
            if message.content_blocks:
                # content_blocks 运行期由 vision_content_blocks 构造（list[dict]），HumanMessage
                # 的 content 类型签名为 list[str | dict]，list 不变性导致 mypy 误报，cast 收口。
                return HumanMessage(content=cast(list, message.content_blocks))
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
            return AIMessage(
                content=content_text,
                tool_calls=langchain_tool_calls,
                additional_kwargs={
                    self._thinking_channel: message.metadata.get("reasoning_content", None)
                },
            )
        if message.role == "tool":
            return ToolMessage(
                content=content_text,
                tool_call_id=message.metadata.get("tool_call_id", ""),
                additional_kwargs={
                    self._thinking_channel: message.metadata.get("reasoning_content", None)
                },
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

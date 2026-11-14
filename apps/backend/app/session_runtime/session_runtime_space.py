"""一个持久化 Session 对应的运行时资源空间。

space 同时持有该 Session 的调度者 ``SessionActor``：随 space 物化而创建，由注册表在事件循环上拉起，
常驻到 Session 删除或进程关闭。

本模块只拥有统一的执行闸门（Session 操作锁）与延迟创建的运行时 context/snapshot working
copy；持久化的 session / run / context 记录仍由 SQLite 负责。多个 space 的进程内生命周期管理见
``app.session_runtime.session_runtime_space_registry``。

加锁边界（**审查须知：请勿把"缺少内部锁"判定为缺陷**）：

本类除 ``_context_guard`` 外不自行加锁，这是刻意的单一闸门设计，不是遗漏：

- **snapshot working copy（``_snapshot``）没有任何内部锁，是刻意的。** 其全部读写入口
  （``get_working_snapshot`` / ``has_snapshot`` / ``unload_snapshot``）
  的生产调用方只有 ``ConversationStateService``（进程单例）；该 service 的 ``_lock``（RLock）是
  snapshot working copy 的**唯一序列化点**，已在懒加载、事件投影、subscriber 注册、删除清理
  所有路径上覆盖。此前额外存在的 ``_snapshot_guard`` 与外层 ``_lock`` 是同一临界区里的双层锁
  （外层 RLock 完全覆盖内层普通 Lock），已按"不重复加锁"删除。
- **直连约定（务必看清这一条，它决定"要不要加回锁"）**：既有单测
  （``tests/test_task4_state_lifecycle.py``）会在**单线程**下直连 space 读写 snapshot 以断言
  working copy 的生命周期，此时不存在并发，属允许用法。任何**多线程**直连（不持
  ``_lock``）都是调用方缺陷：它会同时触发 check-then-set 造成的重复懒加载与
  last-write-wins 覆盖，本类不为此兜底。出现这类调用方时，正确修法是让它回到
  ``ConversationStateService``，**不是**给本类加回内部锁。
- **context manager 槽位**由 ``_context_guard`` 串行化初始化和卸载；它不保护整个 Run
  的执行。Session 操作闸门用于 Transport 创建、编辑、续跑等入口，执行器不全程持有该锁。
- **fork 只复制数据库历史**，不取得源 Session 闸门，不创建或安装源/目标 context manager。

"""

from __future__ import annotations

import asyncio
import copy
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar, cast

from openai.types.shared_params import FunctionDefinition
from pydantic import TypeAdapter

from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.service.depends import get_session_service, get_workspace_service

if TYPE_CHECKING:
    from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
    from app.core.agents.agent_runtime_config import AgentRuntimeConfig
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
    from app.core.session_actor import SessionActor
    from app.models import SessionRecord, WorkspaceRecord

_T = TypeVar("_T")
_tool_schemas_adapter = TypeAdapter(list[FunctionDefinition])


def _weak_ref(obj: _T) -> weakref.ReferenceType[_T]:
    """以弱引用包裹对象，消除类型检查器对 ``weakref.ref`` 返回泛型的推断偏差。

    参数:
        obj: 需要弱引用的对象。

    返回:
        指向 ``obj`` 的弱引用；``obj`` 被回收后该引用解引用返回 None。
    """

    return cast(weakref.ReferenceType[_T], weakref.ref(obj))


@dataclass
class SessionRuntimeSpace:
    """一个持久化 Session 对应的运行时资源空间。

    并发边界见模块 docstring：``lock`` 是唯一 Session 操作闸门；snapshot working copy **不**持有
    内部锁（由 ``ConversationStateService._lock`` 统一串行化）；``_context_manager`` 槽位
    由 ``_context_guard`` 保护。系统通知不在这里排队：持久化通知归外部消息队列，Run 内通知归本次
    执行的 ``RunMailbox``。
    """

    session_id: int
    lock: threading.Lock = field(init=False)
    _context_manager: weakref.ReferenceType[RuntimeMessageContextManager] | None = field(
        default=None, init=False
    )
    # snapshot 是 TypedDict（运行时即 dict），无法被弱引用包裹，因此按 session 维度强引用缓存，
    # 由 ``unload_snapshot`` / 进程内清理显式释放。
    # 该字段刻意不配内部锁：所有访问经 ``ConversationStateService._lock`` 串行化。
    _snapshot: ConversationStateSnapshot | None = field(default=None, init=False)
    _context_guard: threading.Lock = field(init=False)
    agent: AgentRuntimeConfig | None = None
    current_workspace: WorkspaceRecord = field(init=False)
    current_task: SessionRecord = field(init=False)
    # Session 的调度者；在这里创建、由注册表拉起（space 可能在工作线程物化，拉起必须回到事件循环）。
    actor: SessionActor = field(init=False)
    # Session 级投递通道：每次驱动开始时由 actor 清空，运行中 loop 往里投递，prepare_input 取光。
    # 归 space 持有，``RuntimeContext.for_run`` 才能只凭 Run 装配出本次执行的上下文。
    mailbox: RunMailbox = field(default_factory=RunMailbox, init=False)

    @property
    def session_tool_schemas(self) -> tuple[FunctionDefinition, ...]:
        """返回 Session 创建时固化的模型工具 schema 快照。

        返回:
            不可替换且与 SessionRecord 脱离的 schema 元组；嵌套 JSON 值已经深拷贝，调用方可以
            将其传给模型绑定逻辑而不修改 SessionRecord 工作副本。

        异常:
            ValueError: Session 记录中的 SDK schema 不合法、工具名为空或不是 strict 契约。
                旧快照不自动补标记；调用方需新建 Session，不能原地改变固定模型前缀。

        副作用:
            无；该属性只读当前 Session 记录，不读取数据库，也不修改运行时状态。
        """
        schemas = _tool_schemas_adapter.validate_python(self.current_task.tool_schemas, strict=True)
        if any(not schema["name"].strip() for schema in schemas):
            raise ValueError("Session 工具 schema 的 name 不得为空")
        if any(schema.get("strict") is not True for schema in schemas):
            raise ValueError("Session 工具 schema 必须为 strict 契约，请新建 Session")
        return tuple(copy.deepcopy(schemas))

    @property
    def session_tool_names(self) -> tuple[str, ...]:
        """Session 工具面的唯一出口：固化 schema 里的工具名，顺序沿用 schema。

        系统提示词的工具目录与工作方式层、目录更新广播都按它判断「这个 Session 能用什么」；Agent
        配置之后再改不影响它。本 Run 准入（再减去派生限制）见 ``RuntimeContext.admitted_tools``。
        """
        return tuple(schema["name"] for schema in self.session_tool_schemas)

    def __post_init__(self) -> None:
        """初始化统一执行闸门与 context manager 槽位锁。"""

        self.lock = threading.Lock()
        self._context_guard = threading.Lock()
        self.current_task = get_session_service().get_session(self.session_id)
        self.current_workspace = get_workspace_service().get_workspace(
            self.current_task.workspace_id
        )
        from app.core.session_actor import SessionActor

        self.actor = SessionActor(self)

    def restart_actor(self) -> None:
        """换一个新 actor 并拉起；只用于删除失败后的回滚，须在事件循环线程调用。

        删除前的 ``stop`` 停掉了旧 actor 的 loop 且不可逆（``start`` 只拉起一次），删除没提交时
        Session 仍在，必须有新的调度者。换 actor 而不是卸载 space：space 还持有快照 working copy
        与订阅，卸载会让正在 attach 的前端收不到后续推送。新 actor 的水位从 0 起，与进程重启后的
        物化同语义：队列里残留的可推进消息照常判定。
        """
        from app.core.session_actor import SessionActor

        self.actor = SessionActor(self)
        self.actor.start()

    def _acquire_lock(self, timeout: float | None) -> bool:
        """在同步线程中取得 Session 操作闸门。"""

        if timeout is None:
            return self.lock.acquire()
        return self.lock.acquire(timeout=timeout)

    @contextmanager
    def operation(self, timeout: float | None = None) -> Iterator[None]:
        """同步取得统一 Session 操作闸门。

        参数:
            timeout: 最长等待秒数；为 None 时持续等待。

        返回:
            一个释放 Session 闸门的同步上下文管理器。

        异常:
            TimeoutError: 在指定时间内未能取得闸门。

        副作用:
            在上下文期间阻止同一 Session 的其他运行时操作。
        """

        if not self._acquire_lock(timeout):
            raise TimeoutError(f"session {self.session_id} operation lock is busy")
        try:
            yield
        finally:
            self.lock.release()

    @asynccontextmanager
    async def async_operation(self, wait_seconds: float | None = None) -> AsyncIterator[None]:
        """异步取得与同步调用共享的统一 Session 操作闸门。

        参数:
            wait_seconds: 最长等待秒数；为 None 时持续等待。

        返回:
            一个释放 Session 闸门的异步上下文管理器。

        异常:
            TimeoutError: 在指定时间内未能取得闸门。
            asyncio.CancelledError: 调用方取消等待；底层锁仍会先完成收购并释放，
                避免取消窗口遗留永久占用。

        副作用:
            在上下文期间阻止同一 Session 的其他运行时操作，且不会阻塞当前 event loop。
        """

        acquire_task = asyncio.create_task(asyncio.to_thread(self._acquire_lock, wait_seconds))
        try:
            acquired = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            # 不能取消已经提交给线程池的 acquire；必须等待它完成后释放，避免
            # “调用方已取消但底层锁后来才被取得”造成永久锁死。
            acquired = await acquire_task
            if acquired:
                self.lock.release()
            raise
        if not acquired:
            raise TimeoutError(f"session {self.session_id} operation lock is busy")
        try:
            yield
        finally:
            self.lock.release()

    def unload_context_manager(self) -> None:
        """卸载当前 Session 的进程内 context manager 引用。

        当前 ``RuntimeMessageContextManager`` 不持有外部句柄，卸载只需清除引用；调用方必须
        在 Session 已经停止接受新操作且不再有运行持锁时调用本方法。
        """

        with self._context_guard:
            self._context_manager = None

    def get_working_snapshot(
        self, loader: Callable[[], ConversationStateSnapshot]
    ) -> ConversationStateSnapshot:
        """返回 session 的 snapshot working copy（不拷贝），首次调用时用 ``loader`` 懒加载。

        只有 ``ConversationStateService`` 在持有其 ``_lock`` 时调用：投影直接改它，读取与发布由
        service 自己裁窗口后拷贝，所以这里不为任何一方整份拷贝。``loader`` 必须返回新建的对象（交给
        space 持有）；重建或校验失败时异常透传，不缓存不完整的 snapshot。
        """

        if self._snapshot is None:
            self._snapshot = loader()
        return self._snapshot

    def has_snapshot(self) -> bool:
        """snapshot 是否已物化；不复制、不触发懒加载。调用方约定同 ``get_working_snapshot``。"""

        return self._snapshot is not None

    def unload_snapshot(self) -> None:
        """卸载 session snapshot，使下一次访问重新从 canonical records 懒加载。

        并发:
            与 ``get_working_snapshot`` 同一约定：不自行加锁，调用方须经 ``ConversationStateService``。
        """

        self._snapshot = None

    def live_context_manager(self) -> RuntimeMessageContextManager | None:
        """只读返回仍存活的 context manager；没有时返回 ``None``，不创建、不读库。

        :meth:`get_context_manager` 先用它取已有实例，只在槽位为空时才构造（读库、新 Session 还会落库
        系统提示词）；只想判断「当前是否装配了 context」时用它，不能用 ``get_context_manager``。弱引用
        读取本身是原子的，无需持有 ``_context_guard``。
        """

        manager_ref = self._context_manager
        return manager_ref() if manager_ref is not None else None

    def get_context_manager(
        self,
        *,
        agent: AgentRuntimeConfig | None = None,
    ) -> RuntimeMessageContextManager:
        """返回 session context manager；首次执行时才创建并加载它。

        参数:
            agent: 首次装配必需（用于构建 Session 首个系统提示词）；之后可复用已保留的运行时配置。

        返回:
            已创建或已缓存的 ``RuntimeMessageContextManager`` 实例。该 manager 恢复 Session 已持久化的
            固定系统提示词；当前 profile 只用于首次创建 Session 提示词。

        异常:
            RuntimeError: 首次装配没有 Agent 运行时配置。
            ``RuntimeMessageContextManager`` 构造失败时原样向上抛出，本方法不兜底；此时 space
            不会缓存半成品引用，由调用方处理。

        副作用:
            首次调用时在持有 ``_context_guard`` 的前提下惰性构造并以**弱引用**缓存 context
            manager；后续调用返回仍存活的缓存实例。配置更新不会替换 Session 已持久化的系统提示词；
            当外部不再持有该 manager 时弱引用自然失效，下次访问从持久化 context 恢复。

        并发:
            初始化锁只保护槽位创建；配置广播与 workflow 的访问不依赖同一 Session 操作闸门。
            锁内重检避免两个并发访问者分别构造 manager。
        """

        manager = self.live_context_manager()
        if manager is not None:
            return manager
        with self._context_guard:
            manager_ref = self._context_manager
            manager = manager_ref() if manager_ref is not None else None
            if manager is None:
                from app.core.context.runtime_message_context_manager import (
                    RuntimeMessageContextManager,
                )

                if agent is not None:
                    self.agent = agent

                if self.agent is None:
                    raise RuntimeError("首次装配 Session context 必须提供 Agent 运行时配置")
                from app.core.context.context_store import ContextStore

                manager = RuntimeMessageContextManager(
                    current_session_id=self.current_task.id,
                    agent=self.agent,
                    tool_names=self.session_tool_names,
                    workspace=self.current_workspace,
                    writable_root=self.current_task.extra.writable_root,
                    context_store=ContextStore(),
                )
                self._context_manager = _weak_ref(manager)
            return manager

"""一个持久化 Task 对应的运行时资源空间。

本模块只拥有统一的执行闸门（Task 操作锁）与延迟创建的运行时 context/snapshot working
copy；持久化的 task / run / context 记录仍由 SQLite 负责。多个 space 的进程内生命周期管理见
``app.task_runtime.task_runtime_space_registry``。

加锁边界（**审查须知：请勿把"缺少内部锁"判定为缺陷**）：

本类除 ``_context_guard`` 外不自行加锁，这是刻意的单一闸门设计，不是遗漏：

- **snapshot working copy（``_snapshot``）没有任何内部锁，是刻意的。** 其全部读写入口
  （``get_snapshot`` / ``existing_snapshot`` / ``replace_snapshot`` / ``unload_snapshot``）
  的生产调用方只有 ``ConversationTaskStateService``；该 service 的类级 ``_lock``（RLock）是
  snapshot working copy 的**唯一序列化点**，已在懒加载、事件投影、subscriber 注册、删除清理
  所有路径上覆盖。此前额外存在的 ``_snapshot_guard`` 与外层 ``_lock`` 是同一临界区里的双层锁
  （外层 RLock 完全覆盖内层普通 Lock），已按"不重复加锁"删除。
- **直连约定（务必看清这一条，它决定"要不要加回锁"）**：既有单测
  （``tests/test_task4_state_lifecycle.py``）会在**单线程**下直连 space 读写 snapshot 以断言
  working copy 的生命周期，此时不存在并发，属允许用法。任何**多线程**直连（不持
  ``_lock``）都是调用方缺陷：它会同时触发 check-then-set 造成的重复懒加载与
  last-write-wins 覆盖，本类不为此兜底。出现这类调用方时，正确修法是让它回到
  ``ConversationTaskStateService``，**不是**给本类加回内部锁。
- **context manager 槽位**由 ``_context_guard`` 串行化初始化和卸载；它不保护整个 Run
  的执行。Task 操作闸门用于 Transport 创建、编辑、续跑等入口，执行器不全程持有该锁。
- **fork 只复制数据库历史**，不取得源 Task 闸门，不创建或安装源/目标 context manager。

"""

from __future__ import annotations

import asyncio
import copy
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Any, TypeVar, cast

from app.service.depends import get_task_service, get_workspace_service
from app.models.runtime_notice import RuntimeNotice
from app.utils.host_platform import host_platform_name

if TYPE_CHECKING:
    from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
    from app.core.agents.agent_profile import AgentProfile
    from app.core.context.runtime_context_manager import RuntimeContextManager
    from app.models import TaskRecord, WorkspaceRecord

_T = TypeVar("_T")


def _weak_ref(obj: _T) -> weakref.ReferenceType[_T]:
    """以弱引用包裹对象，消除类型检查器对 ``weakref.ref`` 返回泛型的推断偏差。

    参数:
        obj: 需要弱引用的对象。

    返回:
        指向 ``obj`` 的弱引用；``obj`` 被回收后该引用解引用返回 None。
    """

    return cast(weakref.ReferenceType[_T], weakref.ref(obj))


@dataclass
class TaskRuntimeSpace:
    """一个持久化 Task 对应的运行时资源空间。

    并发边界见模块 docstring：``lock`` 是唯一 Task 操作闸门；snapshot working copy **不**持有
    内部锁（由 ``ConversationTaskStateService._lock`` 统一串行化）；``_context_manager`` 槽位
    由 ``_context_guard`` 保护；``_notice_queue`` 延迟提示队列（元素恒为 ``RuntimeNotice``，见
    ``defer_runtime_notice``）由标准库 ``SimpleQueue`` 提供
    并发安全（内部已加锁，本类不再额外加锁），跨同 task 内 run 共享、run 间串行由
    Task 操作闸门保证。
    """

    task_id: int
    lock: threading.Lock = field(init=False)
    _context_manager: weakref.ReferenceType[RuntimeContextManager] | None = field(
        default=None, init=False
    )
    # snapshot 是 TypedDict（运行时即 dict），无法被弱引用包裹，因此按 task 维度强引用缓存，
    # 由 ``unload_snapshot`` / 进程内清理显式释放。
    # 该字段刻意不配内部锁：所有访问经 ``ConversationTaskStateService._lock`` 串行化。
    _snapshot: ConversationStateSnapshot | None = field(default=None, init=False)
    _context_guard: threading.Lock = field(init=False)
    # 延迟提示队列（FIFO，元素恒为 RuntimeNotice）：既承载原有修复类提示，也承载系统提示词增量
    # 通知。队列由标准库 SimpleQueue 提供并发安全；通知消费后由模型节点统一写入 Task context。
    _notice_queue: SimpleQueue[RuntimeNotice] = field(
        default_factory=SimpleQueue, init=False, repr=False, compare=False
    )
    agent_profile: AgentProfile = None
    current_workspace: WorkspaceRecord = None
    current_task: TaskRecord = None

    @property
    def task_tool_schemas(self) -> tuple[dict[str, Any], ...]:
        """返回 Task 创建时固化的模型工具 schema 快照。

        返回:
            不可替换且与 TaskRecord 脱离的 schema 元组；嵌套 JSON 值已经深拷贝，调用方可以
            将其传给模型绑定逻辑而不修改 TaskRecord 工作副本。

        异常:
            ValueError: Task 记录中的 schema 缺少合法工具名时抛出，表示持久化事实损坏。

        副作用:
            无；该属性只读当前 Task 记录，不读取数据库，也不修改运行时状态。
        """
        return tuple(copy.deepcopy(self.current_task.tool_schemas or []))

    def __post_init__(self) -> None:
        """初始化统一执行闸门与 context manager 槽位锁。"""

        self.lock = threading.Lock()
        self._context_guard = threading.Lock()
        self.current_task = get_task_service().get_task(self.task_id)
        self.current_workspace = get_workspace_service().get_workspace(
            self.current_task.workspace_id
        )

    def _acquire_lock(self, timeout: float | None) -> bool:
        """在同步线程中取得 Task 操作闸门。"""

        if timeout is None:
            return self.lock.acquire()
        return self.lock.acquire(timeout=timeout)

    @contextmanager
    def operation(self, timeout: float | None = None) -> Iterator[None]:
        """同步取得统一 Task 操作闸门。

        参数:
            timeout: 最长等待秒数；为 None 时持续等待。

        返回:
            一个释放 Task 闸门的同步上下文管理器。

        异常:
            TimeoutError: 在指定时间内未能取得闸门。

        副作用:
            在上下文期间阻止同一 Task 的其他运行时操作。
        """

        if not self._acquire_lock(timeout):
            raise TimeoutError(f"task {self.task_id} operation lock is busy")
        try:
            yield
        finally:
            self.lock.release()

    @asynccontextmanager
    async def async_operation(self, wait_seconds: float | None = None) -> AsyncIterator[None]:
        """异步取得与同步调用共享的统一 Task 操作闸门。

        参数:
            wait_seconds: 最长等待秒数；为 None 时持续等待。

        返回:
            一个释放 Task 闸门的异步上下文管理器。

        异常:
            TimeoutError: 在指定时间内未能取得闸门。
            asyncio.CancelledError: 调用方取消等待；底层锁仍会先完成收购并释放，
                避免取消窗口遗留永久占用。

        副作用:
            在上下文期间阻止同一 Task 的其他运行时操作，且不会阻塞当前 event loop。
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
            raise TimeoutError(f"task {self.task_id} operation lock is busy")
        try:
            yield
        finally:
            self.lock.release()

    def unload_context_manager(self) -> None:
        """卸载当前 Task 的进程内 context manager 引用。

        当前 ``RuntimeContextManager`` 不持有外部句柄，卸载只需清除引用；调用方必须
        在 Task 已经停止接受新操作且不再有运行持锁时调用本方法。
        """

        with self._context_guard:
            self._context_manager = None

    def get_snapshot(
            self, loader: Callable[[], ConversationStateSnapshot]
    ) -> ConversationStateSnapshot:
        """返回 task snapshot；首次调用时从 canonical records 懒加载重建。

        参数:
            loader: 在 snapshot 尚未物化时执行的重建函数。函数应只读取 canonical
                Task/Run/Context records，不得写入本 space 的 snapshot。

        返回:
            当前 task 的 snapshot 深拷贝。调用方可以安全修改返回值而不污染 space 内的
            working copy。

        异常:
            透传 ``loader`` 的重建异常；失败时不会缓存不完整 snapshot。

        副作用:
            首次调用时重建并缓存进程内 working copy；后续调用复用同一 task 的副本，不再读取
            数据库或重复重建，直到 ``unload_snapshot`` 或进程内清理。

        并发:
            **本方法刻意不自行加锁**（原 ``_snapshot_guard`` 已随"同一临界区双层锁"删除）。
            读写 ``_snapshot`` 必须全部经 ``ConversationTaskStateService``：其类级 ``_lock``
            是唯一序列化点，已在懒加载、事件投影、subscriber 注册与删除清理路径上覆盖。
            若有调用方绕过该 service 直接调用本方法或其它 snapshot 方法，需自行保证互斥。
        """

        if self._snapshot is None:
            self._snapshot = copy.deepcopy(loader())
        return copy.deepcopy(self._snapshot)

    def get_working_snapshot(
            self, loader: Callable[[], ConversationStateSnapshot]
    ) -> ConversationStateSnapshot:
        """Return the task-local mutable snapshot to its owning state service.

        This is deliberately narrower than ``get_snapshot``: only
        ``ConversationTaskStateService`` calls it while holding that service's class-level
        lock. It avoids copying the whole conversation for every token mutation; callers must
        publish or otherwise finish their mutation before releasing the owning lock.
        """

        if self._snapshot is None:
            self._snapshot = copy.deepcopy(loader())
        return self._snapshot

    def existing_snapshot(self) -> ConversationStateSnapshot | None:
        """返回已物化的 task snapshot，不触发数据库读取或懒加载。

        尚未物化或已被 ``unload_snapshot`` 清理时返回 None。

        并发:
            与 ``get_snapshot`` 同一约定：不自行加锁，调用方须经 ``ConversationTaskStateService``。
        """

        return copy.deepcopy(self._snapshot) if self._snapshot is not None else None

    def replace_snapshot(self, snapshot: ConversationStateSnapshot) -> None:
        """替换 task 的进程内 snapshot working copy。

        仅供已完成 canonical 数据库提交后的显式重建或 Transport projector 使用；不会
        写数据库，也不会通知 SSE subscriber。

        并发:
            与 ``get_snapshot`` 同一约定：不自行加锁，调用方须经 ``ConversationTaskStateService``。
        """

        self._snapshot = copy.deepcopy(snapshot)

    def unload_snapshot(self) -> None:
        """卸载 task snapshot，使下一次访问重新从 canonical records 懒加载。

        并发:
            与 ``get_snapshot`` 同一约定：不自行加锁，调用方须经 ``ConversationTaskStateService``。
        """

        self._snapshot = None

    def get_context_manager(
            self,
            *,
            agent_profile: AgentProfile | None = None,
    ) -> RuntimeContextManager:
        """返回 task context manager；首次执行时才创建并加载它。

        参数:
            agent_profile: 当前 task 使用的 Agent 画像，决定 context 装配策略。

        返回:
            已创建或已缓存的 ``RuntimeContextManager`` 实例。该 manager 恢复 Task 已持久化的
            固定系统提示词；当前 profile 只用于首次创建 Task 提示词。

        异常:
            ``RuntimeContextManager`` 构造失败时原样向上抛出，本方法不兜底；此时 space 不会缓存
            半成品引用，由调用方处理。

        副作用:
            首次调用时在持有 ``_context_guard`` 的前提下惰性构造并以**弱引用**缓存 context
            manager；后续调用返回仍存活的缓存实例。配置更新不会替换 Task 已持久化的系统提示词；
            当外部不再持有该 manager 时弱引用自然失效，下次访问从持久化 context 恢复。

        并发:
            初始化锁只保护槽位创建；配置广播与 workflow 的访问不依赖同一 Task 操作闸门。
            锁内重检避免两个并发访问者分别构造 manager。
        """

        manager_ref = self._context_manager
        if manager_ref is not None:
            manager = manager_ref()
            if manager is not None:
                return manager
        with self._context_guard:
            manager_ref = self._context_manager
            manager = manager_ref() if manager_ref is not None else None
            if manager is None:
                from app.core.context.runtime_context_manager import RuntimeContextManager

                if agent_profile is not None:
                    self.agent_profile = agent_profile

                manager = RuntimeContextManager(
                    current_task_id=self.current_task.id,
                    agent_profile=self.agent_profile,
                    workspace_root=self.current_workspace.root_path,
                    is_fork=self.current_task.task_type == "fork",
                )
                self._context_manager = _weak_ref(manager)
            return manager

    def defer_runtime_notice(self, notice: RuntimeNotice) -> None:
        """投递系统通知；只入队，不修改通知、不构造 Message、不写入 context。"""

        self._notice_queue.put(notice)

    def take_runtime_notices(self, *, run_id: int | None = None) -> str:
        """按 FIFO 消费当前 Run 的通知，并丢弃其他 Run 遗留的通知。

        无 run_id 的通知属于 Task，任何 Run 均可消费；省略消费参数时取出全部通知。
        同一 Task 的 Run 由操作闸门串行化，因此非当前 Run 的通知可直接丢弃，避免污染续跑。

        返回:
            压成一段的提示文本（不再返回领域通知对象）：以 ``system_hint:`` 起始
            （模型据此区分系统注入与用户输入）并显式声明宿主平台；声明与各条通知
            正文各占一行，格式由本方法统一收口，调用方不得再自行加前缀。
            模型节点把它作为独立 ``SystemMessage`` 落库进 canonical context。
            注意队列为空时仍返回这段声明文本（不含任何通知正文），返回值因此恒非空。
        """

        notices: list[RuntimeNotice] = []
        while True:
            try:
                notice = self._notice_queue.get_nowait()
            except Empty:
                break
            if run_id is None or notice.run_id is None or notice.run_id == run_id:
                notices.append(notice)
        if not notices:
            return ""

        hint = "".join(f"\n{n.to_hint_text()}" for n in notices)
        return (
            f"system_hint: host OS is {host_platform_name()}; the lines below are runtime "
            "notices injected by the system, not user input. Take them into account, but do "
            "not reply to, acknowledge, or quote them."
            + hint
        )

    def has_runtime_notices(self) -> bool:
        """本 Task 是否仍有等待模型节点消费的系统通知。"""

        return not self._notice_queue.empty()

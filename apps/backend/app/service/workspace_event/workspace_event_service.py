"""Workspace 状态事件编排 service。

单一职责：在 workspace 创建后（两段式第二步），编排一次准备就绪流程——发布
``preparing`` → 调 ``CodeGraphLifecycleService.ensure_ready`` → 发布 ``ready``
或 ``degraded`` → 关闭订阅。同步阻塞（调用方经 ``to_thread`` 跑），Kernel 不可用
降级返回不抛异常。当前准备动作是 CodeGraph 索引就绪；后续可扩展其他 workspace
状态事件而复用本总线的发布/关闭机制。

职责边界：
- 负责：组合 ensure_ready、组装并发布 WorkspaceEvent、finally 关闭订阅。
- 不负责：索引算法（上游）、RPC（Client）、workspace 落库（归 WorkspaceService）、
  事件持久化到 turn 表（本事件走 workspace 级总线，不落 turn 级事件表）。
"""

from app.config.logging.logger import log
from app.models.enums.event_type import EventType
from app.models.event.workspace_event import WorkspaceEvent
from app.models.workspace_readiness import WorkspaceReadiness
from app.service.codegraph_lifecycle_service import CodeGraphLifecycleService
from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus

#: 进入 ensure_ready 前的健康快检超时：Kernel 进程不在/卡死时，不进入最长 600s 的
#: index_init 阻塞，而是立即判定 degraded 并发终态事件，避免前端永久「索引中」。
HEALTH_CHECK_TIMEOUT_SECONDS: float = 5.0


class WorkspaceEventService:
    """编排一次 workspace 准备就绪并广播状态事件。"""

    def __init__(
        self,
        lifecycle: CodeGraphLifecycleService,
        bus: WorkspaceEventBus,
    ) -> None:
        """构造 workspace 事件 service。

        参数:
            lifecycle: CodeGraph 索引生命周期编排服务（同步 ensure_ready）。
            bus: workspace 级状态事件总线。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self._lifecycle = lifecycle
        self._bus = bus

    def prepare(self, workspace_id: str, root_path: str) -> WorkspaceReadiness:
        """发布 preparing → 健康快检 → ensure_ready → 发布 ready/degraded → close。

        参数:
            workspace_id: 所属 workspace 标识（用于总线路由与事件信封）。
            root_path: workspace 根路径（即 CodeGraph 索引根）。

        返回:
            WorkspaceReadiness：ensure_ready 的同步结果（ready 或 degraded）。

        异常:
            无（ensure_ready 绝不抛；事件发布异常仅记录并继续，close 在 finally 保证执行）。

        副作用:
            发布 preparing/ready/degraded 事件到总线；finally 关闭该 workspace 订阅。

        设计要点:
            ensure_ready 在 Kernel 进程不可用时可能一路走到 index_init（最长 600s
            超时，见 Settings.CODEGRAPH_INDEX_INIT_TIMEOUT_SECONDS），期间只发过
            preparing 事件，前端表现为永久「索引中」。因此在进入 ensure_ready 前先
            做一次短超时 ping 快检：Kernel 不可达立即发 WORKSPACE_DEGRADED 并返回，
            绝不进入长阻塞路径。
        """

        self._emit(
            EventType.WORKSPACE_PREPARING,
            workspace_id,
            root_path,
            {"workspace_path": root_path},
        )
        if not self._kernel_reachable(workspace_id, root_path):
            self._emit_degraded(
                workspace_id,
                root_path,
                state="unreachable",
                reason="codegraph kernel unreachable before prepare",
            )
            self._bus.close(workspace_id)
            return WorkspaceReadiness(
                ready=False,
                state="unreachable",
                action_taken="none",
                files_changed=0,
                duration_ms=0,
                degraded_reason="codegraph kernel unreachable before prepare",
            )

        try:
            readiness = self._lifecycle.ensure_ready(root_path)
        except Exception:
            # ensure_ready 约定绝不抛，但防御兜底：异常路径仍关闭订阅避免泄漏，再重抛。
            self._bus.close(workspace_id)
            raise

        # 先发布终态事件，再关闭订阅：close 会清空该 workspace 的订阅桶并写关闭
        # 哨兵（对齐 RuntimeEventBus.close_turn 语义，不再写永久关闭标记）。若先
        # close 再 emit ready/degraded，订阅桶已空、终态事件无人接收，SSE 流只收到
        # preparing 就终止（独立审查暴露的致命时序 bug）。
        if readiness.ready:
            self._emit(
                EventType.WORKSPACE_READY,
                workspace_id,
                root_path,
                {
                    "workspace_path": root_path,
                    "action_taken": readiness.action_taken,
                    "files_changed": readiness.files_changed,
                    "duration_ms": readiness.duration_ms,
                },
            )
        else:
            self._emit_degraded(
                workspace_id,
                root_path,
                state=readiness.state,
                reason=readiness.degraded_reason or "",
            )
        self._bus.close(workspace_id)
        return readiness

    def _kernel_reachable(self, workspace_id: str, root_path: str) -> bool:
        """在 prepare 前快速探测 Kernel 是否可达，避免进入长阻塞 index_init。

        参数:
            workspace_id: workspace 标识（仅用于日志上下文）。
            root_path: workspace 根路径（仅用于日志上下文）。

        返回:
            bool: True 表示 Kernel 进程存活且可 ping 通；False 表示不可达（应直接 degraded）。

        异常:
            无（任何异常一律视为不可达，走降级路径）。

        副作用:
            调用 lifecycle 持有的 kernel client 做一次短超时 ping；不发布任何事件。
        """

        client = self._lifecycle.get_client()
        if client is None:
            return False
        try:
            client.ping(timeout=HEALTH_CHECK_TIMEOUT_SECONDS)
            return True
        except Exception as exc:
            log.warning(
                "workspace_event_kernel_unreachable",
                extra={
                    "msg": "prepare 前 Kernel 健康快检失败，直接降级",
                    "data": {
                        "workspace_id": workspace_id,
                        "root_path": root_path,
                        "error": str(exc),
                    },
                },
            )
            return False

    def _emit_degraded(
        self,
        workspace_id: str,
        workspace_path: str,
        state: str,
        reason: str,
    ) -> None:
        """统一发布 WORKSPACE_DEGRADED 终态事件（被 prepare 的多条降级路径复用）。

        参数:
            workspace_id: workspace 标识。
            workspace_path: workspace 根路径。
            state: Kernel 当前状态（如 unreachable / error / degraded）。
            reason: 降级原因（脱敏，不记录 secret）。

        返回:
            无。

        异常:
            无（发布失败仅记录日志并继续）。

        副作用:
            向总线分发一条 WORKSPACE_DEGRADED 事件。
        """

        self._emit(
            EventType.WORKSPACE_DEGRADED,
            workspace_id,
            workspace_path,
            {
                "workspace_path": workspace_path,
                "state": state,
                "degraded_reason": reason,
            },
        )

    def _emit(
        self,
        event_type: EventType,
        workspace_id: str,
        workspace_path: str,
        payload: dict[str, object],
    ) -> None:
        """构造并发布一条 workspace 状态事件。

        参数:
            event_type: 事件类型（preparing/ready/degraded）。
            workspace_id: workspace 标识。
            workspace_path: workspace 根路径。
            payload: 事件负载字典（对齐对应 Workspace*Payload 字段）。

        返回:
            无。

        异常:
            无（发布失败仅记录日志并继续，不阻断 prepare）。

        副作用:
            向总线分发一条 WorkspaceEvent。
        """

        try:
            self._bus.publish(
                WorkspaceEvent(
                    event_type=event_type,
                    workspace_id=workspace_id,
                    workspace_path=workspace_path,
                    payload=payload,
                )
            )
        except Exception:
            log.exception(
                "workspace_event_emit_failed",
                extra={
                    "msg": "workspace 状态事件发布失败",
                    "data": {"workspace_id": workspace_id, "event_type": event_type.value},
                },
            )

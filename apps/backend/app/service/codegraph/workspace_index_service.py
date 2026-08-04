"""Workspace 索引进度编排 service。

单一职责：在 workspace 创建后（两段式第二步），编排一次索引就绪准备——发布
``preparing`` → 调 ``CodeGraphLifecycleService.ensure_ready`` → 发布 ``ready``
或 ``degraded`` → 关闭订阅。同步阻塞（调用方经 ``to_thread`` 跑），Kernel 不可用
降级返回不抛异常。

职责边界：
- 负责：组合 ensure_ready、组装并发布 WorkspaceIndexEvent、finally 关闭订阅。
- 不负责：索引算法（上游）、RPC（Client）、workspace 落库（归 WorkspaceService）、
  事件持久化到 turn 表（本事件走 workspace 级总线，不落 turn 级事件表）。
"""

from app.config.logging.logger import log
from app.models.enums.event_type import EventType
from app.models.workspace_index_event import WorkspaceIndexEvent
from app.models.workspace_index_readiness import WorkspaceIndexReadiness
from app.service.codegraph.lifecycle_service import CodeGraphLifecycleService
from app.service.codegraph.workspace_index_bus import WorkspaceIndexBus


class WorkspaceIndexService:
    """编排一次 workspace 索引就绪准备并广播进度事件。"""

    def __init__(
        self,
        lifecycle: CodeGraphLifecycleService,
        bus: WorkspaceIndexBus,
    ) -> None:
        """构造索引准备 service。

        参数:
            lifecycle: CodeGraph 索引生命周期编排服务（同步 ensure_ready）。
            bus: workspace 级索引进度事件总线。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self._lifecycle = lifecycle
        self._bus = bus

    def prepare(self, workspace_id: str, root_path: str) -> WorkspaceIndexReadiness:
        """发布 preparing → ensure_ready → 发布 ready/degraded → close。

        参数:
            workspace_id: 所属 workspace 标识（用于总线路由与事件信封）。
            root_path: workspace 根路径（即 CodeGraph 索引根）。

        返回:
            WorkspaceIndexReadiness：ensure_ready 的同步结果（ready 或 degraded）。

        异常:
            无（ensure_ready 绝不抛；事件发布异常仅记录并继续，close 在 finally 保证执行）。

        副作用:
            发布 preparing/ready/degraded 事件到总线；finally 关闭该 workspace 订阅。
        """

        self._emit(
            EventType.WORKSPACE_PREPARING,
            workspace_id,
            root_path,
            {"workspace_path": root_path},
        )
        try:
            readiness = self._lifecycle.ensure_ready(root_path)
        except Exception:
            # ensure_ready 约定绝不抛，但防御兜底：异常路径仍关闭订阅避免泄漏，再重抛。
            self._bus.close(workspace_id)
            raise

        # 先发布终态事件，再关闭订阅：close 会把 workspace_id 标记为已关闭，
        # 其后的 publish 为 noop。若先 close 再 emit ready/degraded，终态事件会被吞掉，
        # SSE 流只收到 preparing 就终止（独立审查暴露的致命时序 bug）。
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
            self._emit(
                EventType.WORKSPACE_DEGRADED,
                workspace_id,
                root_path,
                {
                    "workspace_path": root_path,
                    "state": readiness.state,
                    "degraded_reason": readiness.degraded_reason or "",
                },
            )
        self._bus.close(workspace_id)
        return readiness

    def _emit(
        self,
        event_type: EventType,
        workspace_id: str,
        workspace_path: str,
        payload: dict[str, object],
    ) -> None:
        """构造并发布一条 workspace 索引进度事件。

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
            向总线分发一条 WorkspaceIndexEvent。
        """

        try:
            self._bus.publish(
                WorkspaceIndexEvent(
                    event_type=event_type,
                    workspace_id=workspace_id,
                    workspace_path=workspace_path,
                    payload=payload,
                )
            )
        except Exception:
            log.exception(
                "workspace_index_emit_failed",
                extra={
                    "msg": "workspace 索引进度事件发布失败",
                    "data": {"workspace_id": workspace_id, "event_type": event_type.value},
                },
            )

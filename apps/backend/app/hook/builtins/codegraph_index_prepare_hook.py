"""Turn 前 CodeGraph 索引保活 Hook（USER_PROMPT_SUBMIT）。

单一职责：在每次 turn 即将运行（用户 prompt 已提交、task 已认领、RUN_STARTED
之前）时，确保该 workspace 的 CodeGraph 索引是最新的。复用
``CodeGraphLifecycleService.ensure_ready``（其内部已编排 status → init/sync
并做并发 singleflight 去重），不再造一遍「按需同步」逻辑。

职责边界：
- 负责：workspace_id → path 解析、调用 ensure_ready、异常兜底放行、记日志。
- 不负责：索引算法（上游 CodeGraph）、RPC（Client）、并发去重
  （InflightRegistry，已在 LifecycleService 内）、事件发布（客户端无需感知，
  显式不发布 workspace 状态事件）、workspace 创建即索引（归 WorkspaceEventService）。

失败安全语义：Hook 本身异常或被 ``HookBase.fire`` 兜底为 ALLOW 均不阻断主流程
（与既有内置 Hook 一致，符合用户「CodeGraph 一般不会失败，直接挂载」的诉求）。
"""

from app.config.logging.logger import log
from app.hook import HookContext
from app.hook.hook_base import HookBase
from app.hook.hook_event import HookEvent
from app.hook.hook_result import HookResult
from app.service.codegraph_lifecycle_service import CodeGraphLifecycleService
from app.service.task.workspace_service import WorkspaceService


class CodeGraphIndexPrepareHook(HookBase):
    """Turn 前保活 CodeGraph 索引的内置 Hook。

    挂载于 ``HookEvent.USER_PROMPT_SUBMIT``，每次 turn 前确保 workspace 索引最新。
    """

    def __init__(
        self,
        lifecycle_service: CodeGraphLifecycleService,
        workspace_service: WorkspaceService,
    ) -> None:
        """构造索引保活 Hook。

        参数:
            lifecycle_service: CodeGraph 索引生命周期编排服务（同步 ensure_ready）。
            workspace_service: 工作区 service，用于 workspace_id → root_path 解析。

        返回:
            无。

        异常:
            ValueError: 基类若传入非法 matcher（本处固定 ``matcher=None``，正常不抛）。

        副作用:
            固化基类 ``event=USER_PROMPT_SUBMIT`` / ``matcher=None`` 只读属性（必须
            调用 ``super().__init__``，否则 ``matches()`` 访问 ``_compiled`` 抛
            ``AttributeError``，导致 Hook 永不执行）。
        """
        super().__init__(event=HookEvent.USER_PROMPT_SUBMIT, matcher=None)
        self._lifecycle = lifecycle_service
        self._workspace_service = workspace_service

    def execute(self, context: HookContext) -> HookResult:
        """Turn 前保活 CodeGraph 索引。

        流程：无 workspace_id → 跳过；解析 workspace_path；client 不可用 → 跳过
        （降级到文件搜索）；ensure_ready 同步保活索引；任何异常 → error 日志 + ALLOW。

        参数:
            context: 运行时注入的 ``HookContext``（含 workspace_id / task_id / run_id）。

        返回:
            HookResult.allow()：本 Hook 永不阻断主流程（索引准备是旁路优化，
            失败安全）。

        异常:
            不向外抛出：所有异常在内部吞掉并记为 error 日志，保证 ALLOW。

        副作用:
            可能触发 CodeGraph 索引 init/sync（经 lifecycle）；写 info/debug/error 日志。
        """
        workspace_id = context.workspace_id
        run_id = context.run_id

        if not workspace_id:
            log.debug(
                "codegraph_prepare_skip_no_workspace",
                extra={
                    "msg": "Hook 上下文无 workspace_id，跳过 CodeGraph 索引保活",
                    "data": {"run_id": run_id},
                },
            )
            return HookResult.allow()

        try:
            workspace = self._workspace_service.get_workspace(workspace_id)
        except Exception:
            log.exception(
                "codegraph_prepare_resolve_path_failed",
                extra={
                    "msg": "解析 workspace 路径失败，跳过 CodeGraph 索引保活",
                    "data": {"workspace_id": workspace_id, "run_id": run_id},
                },
            )
            return HookResult.allow()

        workspace_path = getattr(workspace, "root_path", None)
        if not workspace_path:
            log.debug(
                "codegraph_prepare_skip_empty_path",
                extra={
                    "msg": "workspace 路径为空，跳过 CodeGraph 索引保活",
                    "data": {"workspace_id": workspace_id, "run_id": run_id},
                },
            )
            return HookResult.allow()

        # Kernel 不可用（client 未注入）时 ensure_ready 内部降级为 ready=False，
        # 不抛异常；仍返回 ALLOW，主流程不受影响（降级到文件搜索）。
        try:
            readiness = self._lifecycle.ensure_ready(workspace_path)
        except Exception:
            log.exception(
                "codegraph_prepare_ensure_failed",
                extra={
                    "msg": "CodeGraph 索引保活失败，放行主流程",
                    "data": {
                        "workspace_id": workspace_id,
                        "workspace_path": workspace_path,
                        "run_id": run_id,
                    },
                },
            )
            return HookResult.allow()

        if readiness.ready:
            log.info(
                "codegraph_prepare_ok",
                extra={
                    "msg": "CodeGraph 索引保活完成",
                    "data": {
                        "workspace_id": workspace_id,
                        "workspace_path": workspace_path,
                        "action_taken": readiness.action_taken,
                        "files_changed": readiness.files_changed,
                        "duration_ms": readiness.duration_ms,
                        "run_id": run_id,
                    },
                },
            )
        else:
            log.warning(
                "codegraph_prepare_degraded",
                extra={
                    "msg": "CodeGraph 索引保活降级，放行主流程（客户端不感知）",
                    "data": {
                        "workspace_id": workspace_id,
                        "workspace_path": workspace_path,
                        "state": readiness.state,
                        "degraded_reason": readiness.degraded_reason,
                        "run_id": run_id,
                    },
                },
            )
        return HookResult.allow()

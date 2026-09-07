"""内置 Hook 播种入口。

单一职责：把首版内置 Hook 注册进给定的 ``HookRegistry``。无配置层（决策 D3），
内置 Hook 在启动期硬编码播种。当前内置 Hook：

- ``CodeGraphIndexPrepareHook``：每次 turn 前保活 CodeGraph 索引（USER_PROMPT_SUBMIT）。
- ``FileSnapshotHook``：文件类工具成功后采集回退快照（POST_TOOL_USE）。

CodeGraph 不可用时（Kernel 未就绪 / client 未注入），``CodeGraphIndexPrepareHook``
的 ``ensure_ready`` 内部降级为 ``ready=False`` 且不抛异常，注册阶段构造失败也应
静默跳过，避免阻断其他 Hook 播种（失败安全）。
"""

from __future__ import annotations

from app.config.logging.logger import log
from app.config.settings import Settings
from app.hook.builtins.codegraph_index_prepare_hook import (
    CodeGraphIndexPrepareHook,
)
from app.hook.builtins.file_snapshot_hook import FileSnapshotHook
from app.hook.hook_registry import HookRegistry


def bootstrap_hooks(registry: HookRegistry) -> None:
    """把首版内置 Hook 注册进注册表。

    参数:
        registry: 待播种的 ``HookRegistry`` 实例（由 ``initialize_hook_registry`` 传入）。

    返回:
        无。

    异常:
        无（单个 Hook 构造/注册失败不阻断其余 Hook 播种，记 error 日志）。

    副作用:
        向 ``registry`` 写入内置 Hook 订阅（运行期只读，仅启动期调用一次）。
    """
    _register_codegraph_prepare_hook(registry)
    _register_file_snapshot_hook(registry)


def _register_file_snapshot_hook(registry: HookRegistry) -> None:
    """注册文件快照采集 Hook（POST_TOOL_USE）。

    该 Hook 为系统内部数据流水线（文件回退快照），无业务副作用、失败安全；
    作为启动期强制播种的内置 Hook，运行期只读，不会被用户裁剪。

    参数:
        registry: 待播种的 ``HookRegistry`` 实例。

    返回:
        无。

    异常:
        不向外抛出：构造/注册异常一律记 error 日志后吞掉。

    副作用:
        向 ``registry`` 写入一条 POST_TOOL_USE 订阅。
    """
    try:
        registry.register(FileSnapshotHook())
    except Exception:
        log.exception(
            "file_snapshot_hook_register_failed",
            extra={"msg": "文件快照 Hook 注册失败，跳过（不影响主流程）"},
        )


def _register_codegraph_prepare_hook(registry: HookRegistry) -> None:
    """构造并注册 CodeGraph 索引保活 Hook；CodeGraph 不可用时静默跳过。

    CodeGraph 总开关（``Settings.CODEGRAPH_ENABLED``）关闭时直接跳过注册，
    不构造 lifecycle、不触碰 Kernel supervisor，避免无效注册与降级日志噪声。

    Kernel 未就绪（supervisor 未初始化 / client 未注入）时不可构造
    ``CodeGraphLifecycleService``，此时直接跳过注册，不影响主流程与 ToolAuditHook。

    参数:
        registry: 待播种的 ``HookRegistry`` 实例。

    返回:
        无。

    异常:
        不向外抛出：构造/注册异常一律记 error 日志后吞掉。

    副作用:
    CodeGraph 可用时向 ``registry`` 写入一条 USER_PROMPT_SUBMIT 订阅。
    """
    if not Settings.CODEGRAPH_ENABLED:
        # 总开关关闭：不挂载 CodeGraph 索引保活 Hook（默认关闭）。
        return
    try:
        from app.codegraph import CodeGraphKernelUnavailableError, get_kernel_supervisor
        from app.service.codegraph_lifecycle_service import CodeGraphLifecycleService
        from app.service.depends import get_workspace_service

        try:
            client = get_kernel_supervisor().get_client()
        except (RuntimeError, CodeGraphKernelUnavailableError):
            # supervisor 未初始化或 Kernel 未就绪：禁用索引保活，降级到文件搜索。
            log.info(
                "codegraph_prepare_hook_skipped",
                extra={"msg": "CodeGraph Kernel 不可用，跳过索引保活 Hook 注册"},
            )
            return
        lifecycle = CodeGraphLifecycleService(client)
        workspace_service = get_workspace_service()
    except Exception:
        log.exception(
            "codegraph_prepare_hook_register_failed",
            extra={"msg": "CodeGraph 索引保活 Hook 注册失败，跳过（不影响主流程）"},
        )
        return

    registry.register(CodeGraphIndexPrepareHook(lifecycle, workspace_service))

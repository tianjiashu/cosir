"""内置 Hook 播种入口。

单一职责：把首版内置 Hook 注册进给定的 ``HookRegistry``。无配置层（决策 D3），
内置 Hook 在启动期硬编码播种。当前两个内置 Hook：

- ``ToolAuditHook``：验证 Hook 机制最小可用性（事件型，无业务副作用）。
- ``CodeGraphIndexPrepareHook``：每次 turn 前保活 CodeGraph 索引（USER_PROMPT_SUBMIT）。

CodeGraph 不可用时（Kernel 未就绪 / client 未注入），``CodeGraphIndexPrepareHook``
的 ``ensure_ready`` 内部降级为 ``ready=False`` 且不抛异常，注册阶段构造失败也应
静默跳过，避免阻断其他 Hook 播种（失败安全）。
"""

from __future__ import annotations

from app.config.logging.logger import log
from app.hook.builtins.codegraph_index_prepare_hook import (
    CodeGraphIndexPrepareHook,
)
from app.hook.builtins.tool_audit_hook import ToolAuditHook
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
    registry.register(ToolAuditHook())
    _register_codegraph_prepare_hook(registry)


def _register_codegraph_prepare_hook(registry: HookRegistry) -> None:
    """构造并注册 CodeGraph 索引保活 Hook；CodeGraph 不可用时静默跳过。

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

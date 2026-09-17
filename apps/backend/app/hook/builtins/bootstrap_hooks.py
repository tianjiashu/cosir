"""内置 Hook 播种入口。

单一职责：把首版内置 Hook 注册进给定的 ``HookRegistry``。无配置层（决策 D3），
内置 Hook 在启动期硬编码播种。当前内置 Hook：

- ``FileSnapshotHook``：文件类工具成功后采集回退快照（POST_TOOL_USE）。
"""

from __future__ import annotations

from app.config.logging.logger import log
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

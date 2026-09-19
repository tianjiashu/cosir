"""ChangeSet 删除暂存区的路径约定与落盘/清理原语。

删除类操作不把被删文件读入内存、也不写 blob：落盘删除前文件被 ``rename`` 到
workspace 内的 ``.cosir/trash/<operation_id>/<relative>``（同卷元数据操作，O(1)，
对二进制/大文件零额外内存与磁盘拷贝）。该副本即回退事实：``keep`` 时整目录 purge、
``revert`` 时整文件移回原路径。

本模块只描述 trash 引用的解析与少量原子落盘动作，不承载 ChangeSet 业务编排；放在
``core/tools/guard`` 包内以便守卫与文件工具（core）以及变更集服务（service）共用，
不引入 core↔service 反向依赖。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.config.logging.logger import log

_COSIR_DIR_NAME = ".cosir"
_TRASH_DIR_NAME = "trash"
_TRASH_REF_PREFIX = "trash:"
_TRASH_REF_SEP = "/"


def trash_root_for(workspace_root: Path, operation_id: str) -> Path:
    """返回某个操作专用暂存目录的绝对路径（目录本身可能尚未创建）。

    参数:
        workspace_root: 已解析的工作区边界根路径。
        operation_id: 一次文件操作的唯一标识（``mut_<hex>``）。

    返回:
        ``<workspace_root>/.cosir/trash/<operation_id>`` 的绝对路径。

    异常:
        无。

    副作用:
        无（不创建目录）。
    """

    return Path(workspace_root).resolve() / _COSIR_DIR_NAME / _TRASH_DIR_NAME / operation_id


def trash_restore_ref(operation_id: str, relative_path: str) -> str:
    """构造可被回退逻辑解析的 trash 引用。

    参数:
        operation_id: 一次文件操作的唯一标识。
        relative_path: 工作区相对路径（POSIX 分隔符）。

    返回:
        ``trash:<operation_id>/<relative_path>`` 形式的引用字符串。

    异常:
        无。

    副作用:
        无。
    """

    return f"{_TRASH_REF_PREFIX}{operation_id}{_TRASH_REF_SEP}{relative_path}"


def parse_trash_ref(restore_ref: str) -> tuple[str, str] | None:
    """从 ``trash:<operation_id>/<relative>`` 解析出 ``(operation_id, relative)``。

    参数:
        restore_ref: 待解析的引用字符串。

    返回:
        成功为 ``(operation_id, relative)``；非 trash 引用或格式非法时为 ``None``。

    异常:
        无。

    副作用:
        无。
    """

    if not restore_ref.startswith(_TRASH_REF_PREFIX):
        return None
    body = restore_ref[len(_TRASH_REF_PREFIX):]
    separator_index = body.find(_TRASH_REF_SEP)
    if separator_index <= 0:
        return None
    operation_id = body[:separator_index]
    relative = body[separator_index + 1:]
    if not operation_id or not relative or "/" in operation_id or "\\" in operation_id:
        return None
    return operation_id, relative


def resolve_trash_path(workspace_root: Path, restore_ref: str) -> Path | None:
    """解析 trash 引用的实际磁盘路径；非 trash 引用返回 ``None``。

    参数:
        workspace_root: 工作区根路径。
        restore_ref: 待解析的引用字符串。

    返回:
        trash 文件的实际绝对路径；非 trash 引用时为 ``None``。

    异常:
        无。

    副作用:
        无。
    """

    parsed = parse_trash_ref(restore_ref)
    if parsed is None:
        return None
    operation_id, relative = parsed
    return trash_root_for(workspace_root, operation_id) / relative


def move_path_to_trash(source: Path, trash_target: Path) -> None:
    """把源文件移动到 trash 目标（同卷 ``os.replace``，必要时建父目录）。

    参数:
        source: 待移走的源文件路径（尚在工作区中）。
        trash_target: 目标 trash 路径；父目录会被创建。

    返回:
        无。

    异常:
        OSError: 父目录创建或 rename 失败。

    副作用:
        改写目录项：建立 trash 目标并移除源路径；移动失败时源文件保持原位。
    """

    trash_target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, trash_target)


def purge_trash(workspace_root: Path, operation_id: str) -> None:
    """确认删除后清理某操作的暂存目录（``keep`` 路径使用）。

    参数:
        workspace_root: 工作区根路径。
        operation_id: 待清理的操作标识。

    返回:
        无。

    异常:
        无（清理失败仅记警告日志，不阻断 keep；磁盘残留由后续 GC 兜底）。

    副作用:
        递归删除 ``.cosir/trash/<operation_id>`` 整个目录（若存在）。
    """

    root = trash_root_for(workspace_root, operation_id)
    if not root.exists():
        return
    try:
        shutil.rmtree(root)
    except OSError as exc:
        log.warning(
            "task_change_set_trash_purge_failed",
            extra={
                "msg": "确认删除后未能清理 trash 暂存目录，残留由后续 GC 兜底",
                "data": {"operation_id": operation_id, "error": str(exc)},
            },
            exc_info=True,
        )


def restore_from_trash(trash_target: Path, workspace_target: Path) -> None:
    """从 trash 把文件移回 workspace 原路径（``revert`` 路径使用）。

    参数:
        trash_target: 暂存区内的被删文件。
        workspace_target: 工作区内的原始目标路径；父目录会被创建。

    返回:
        无。

    异常:
        OSError: 目标父目录创建或 rename 失败。

    副作用:
        把文件移回原路径；移出后自该文件所在目录向上清理空目录，直至（含）本操作的
        暂存根目录；若该根内仍有其他待回退副本则不整体删除，避免误删。
    """

    workspace_target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(trash_target, workspace_target)
    _prune_empty_trash_dirs(trash_target)


def _prune_empty_trash_dirs(trash_target: Path) -> None:
    """回退后自被移出文件所在目录向上删除空目录，直到（含）操作暂存根。

    若到达操作暂存根时其内仍有其他待回退副本（同一 operation_id 暂存多文件的未来场景），
    则保留该根并停止；仅清理因本次回退而变空的目录分支。清理失败仅记警告，不阻断主流程。
    """

    start = trash_target.parent.resolve()
    # 操作暂存根目录是 ``.cosir/trash`` 的直接子目录。
    root = start
    while root.parent != root and root.parent.name != _TRASH_DIR_NAME:
        root = root.parent
    current = start
    while True:
        if current == root:
            _try_rmdir(root)
            break
        if not _try_rmdir(current):
            break
        current = current.parent


def _try_rmdir(directory: Path) -> bool:
    """尝试删除空目录；非空则保留，异常则记警告返回 ``False``。"""

    try:
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
        return True
    except OSError as exc:
        log.warning(
            "task_change_set_trash_prune_failed",
            extra={
                "msg": "回退后清理 trash 空目录失败，残留可忽略",
                "data": {"directory": str(directory), "error": str(exc)},
            },
            exc_info=True,
        )
        return False


__all__ = [
    "parse_trash_ref",
    "purge_trash",
    "resolve_trash_path",
    "restore_from_trash",
    "trash_restore_ref",
    "trash_root_for",
]

"""可写根：一个 Session 的写类文件工具能写的目录，以及派生时「只能收窄」的规则。

事实存在 ``SessionExtra.writable_root``（相对工作区根的 POSIX 路径，``None`` 表示整个工作区），创建
Session 时定、之后不变，所以「在上级之内」只在派生那一刻校验一次（:func:`narrow`），不沿派生链现算。
它只收窄可写区与终端的缺省工作目录：相对路径仍按工作区根解析，读不受它限制——Session 树与界面
（链接、文件面板）共用一套坐标，可写根不是另一个工作区。

消费方：``PathResolver.resolve_for_write``（写类文件工具）、``execute_terminal`` 的缺省工作目录、
系统提示词与分叉交接通知。命令本身还没有沙箱，终端里写到可写根之外目前拦不住（AGENTS 待办
「安全与权限」）。
"""

from __future__ import annotations

from pathlib import Path

from app.core.permission.protected_paths import ProtectedPaths
from app.utils.path.validation import is_within, is_within_cosir


class WritableRootError(ValueError):
    """派生时请求的可写根不合法；消息是面向模型的英文短句，写明上限。"""


def absolute(workspace_root: str | Path, writable_root: str | None) -> Path:
    """把 Session 事实还原成绝对目录；未收窄时就是工作区根。"""

    root = Path(workspace_root)
    return root if writable_root is None else root / writable_root


def narrow(workspace_root: str | Path, current: str | None, requested: str) -> str | None:
    """校验派生方请求的可写根并返回要存进新 Session 的值。

    ``requested`` 相对工作区根解析（也接受绝对路径），必须是已存在的目录、落在派生方自己的可写根
    ``current`` 之内（含相等），且不在工作区 ``.cosir`` 或受保护路径里。请求的正是工作区根时返回
    ``None``（没有收窄）。

    异常:
        WritableRootError: 上述任一条不满足。
    """

    workspace = Path(workspace_root).resolve()
    ceiling = absolute(workspace, current).resolve()
    candidate = Path(requested)
    try:
        resolved = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
    except (OSError, RuntimeError) as exc:
        raise WritableRootError(f"writable_root cannot be resolved: {requested} ({exc})") from exc
    if not is_within(resolved, ceiling):
        raise WritableRootError(
            f"writable_root must be inside your own writable root {ceiling}: {requested}. "
            "A derived session can only be narrowed, never widened."
        )
    if not resolved.is_dir():
        raise WritableRootError(f"writable_root is not an existing directory: {requested}")
    if is_within_cosir(resolved, workspace) or ProtectedPaths.current().denies_write(resolved):
        raise WritableRootError(f"writable_root is inside a protected area: {requested}")
    return None if resolved == workspace else resolved.relative_to(workspace).as_posix()

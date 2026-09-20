"""``.cosir`` 目录路径计算与保留子树判定（leaf 层纯路径工具）。

单一职责：给出 workspace 级与系统级 ``.cosir`` 目录及其子目录的路径，并判定「某个路径是否
落在 workspace ``.cosir`` 保留子树内」。只做路径运算，**不创建目录、不读写文件、不依赖业务模型**。

为什么集中：``.cosir`` 基名与子目录规则此前在 ``workspace_service``、``attachment_service``、
``image_utils`` 三处各自拼接，口径容易漂移；本模块是唯一允许拼接 ``.cosir`` 基名的位置。

不负责：

- 目录创建：workspace 级见 ``WorkspaceService._init_cosir_metadata``，系统级见后端 lifespan。
- 附件与工具输出 artifact 的实际读写：分别见 ``service.attachment.attachment_service`` 与
  ``core.tools.guard.tool_output_budget``。
- 系统级路径的落点决策：系统级 ``.cosir`` 来自 ``app.utils.paths.SYSTEM_COSIR_DIR``；``.cosir``
  **目录名固定**为本模块的 ``COSIR_DIR_NAME`` 常量，不作为配置项。
"""

from __future__ import annotations

import os
from pathlib import Path

COSIR_DIR_NAME: str = ".cosir"
COSIR_ATTACHMENT_DIR_NAME: str = "Attachment"
COSIR_ATTACHMENT_STAGING_DIR_NAME: str = ".uploading"
COSIR_TOOL_ARTIFACT_DIR_NAME: str = "tool-artifacts"


def workspace_cosir_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 内 ``.cosir`` 目录路径。

    参数:
        workspace_root: 工作区根目录（字符串或 ``Path``）。

    返回:
        ``<workspace_root>/.cosir``；不做 ``resolve``、不校验存在性。

    异常:
        无。

    副作用:
        无（纯路径拼接，不访问文件系统）。
    """

    return Path(workspace_root) / COSIR_DIR_NAME


def workspace_attachment_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 附件目录路径。

    参数:
        workspace_root: 工作区根目录（字符串或 ``Path``）。

    返回:
        ``<workspace_root>/.cosir/Attachment``；不校验存在性。

    异常:
        无。

    副作用:
        无。
    """

    return workspace_cosir_dir(workspace_root) / COSIR_ATTACHMENT_DIR_NAME


def workspace_attachment_staging_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 附件上传暂存目录路径。

    参数:
        workspace_root: 工作区根目录（字符串或 ``Path``）。

    返回:
        ``<workspace_root>/.cosir/Attachment/.uploading``；不校验存在性。

    异常:
        无。

    副作用:
        无。
    """

    return workspace_attachment_dir(workspace_root) / COSIR_ATTACHMENT_STAGING_DIR_NAME


def workspace_tool_artifact_dir(workspace_root: str | Path) -> Path:
    """返回 workspace 工具输出 artifact 目录路径。

    参数:
        workspace_root: 工作区根目录（字符串或 ``Path``）。

    返回:
        ``<workspace_root>/.cosir/tool-artifacts``；不校验存在性。

    异常:
        无。

    副作用:
        无。
    """

    return workspace_cosir_dir(workspace_root) / COSIR_TOOL_ARTIFACT_DIR_NAME


def is_within_cosir(path: str | Path, workspace_root: str | Path | None) -> bool:
    """判断路径（含符号链接解析后）是否落在 workspace ``.cosir`` 保留子树内。

    与写工具 ``PathResolver`` 的 workspace containment 规则互补：containment 保证「不出工作区」，
    本函数保证「不进入 ``.cosir`` 保留区」。判定在 ``realpath`` + ``normcase`` 之后按**路径分量**
    比较，因此（a）指向 ``.cosir`` 的符号链接会命中，（b）大小写不敏感文件系统按真实大小写命中，
    （c）``.cosir2`` 这类前缀相同但不同名的兄弟目录不会误命中。

    参数:
        path: 待判断的路径；允许尚不存在的写目标（``realpath`` 只解析已存在的祖先）。空串或
            纯空白视为无效路径，直接返回 ``False``（避免落到 ``realpath("")`` 的当前目录语义）。
        workspace_root: 工作区根路径；为 ``None`` 或空串时视为无 workspace，直接返回 ``False``。

    返回:
        ``True`` 表示 ``path`` 等于 ``<workspace_root>/.cosir`` 或位于其下；否则 ``False``。

    异常:
        无：``OSError``（路径非法、无法解析）归一化为 ``False``。

    副作用:
        无（只解析路径字符串，不读写文件）。
    """

    text = str(path)
    if not workspace_root or not text.strip():
        return False
    try:
        cosir_root = os.path.normcase(os.path.realpath(str(workspace_cosir_dir(workspace_root))))
        candidate = os.path.normcase(os.path.realpath(text))
    except OSError:
        return False
    return candidate == cosir_root or candidate.startswith(cosir_root + os.sep)


def system_cosir_dir() -> Path:
    """返回系统级 ``.cosir`` 目录路径（``<数据根>/.cosir``）。

    目录名 ``.cosir`` 是**固定常量**（``COSIR_DIR_NAME``），不对外配置；数据根来自
  ``app.utils.paths.SYSTEM_COSIR_DIR``（由 ``CODING_AGENT_DATA_DIR`` 推导）。

    返回:
        系统级 ``.cosir`` 目录的 ``Path``；不校验存在性。

    异常:
        无。

    副作用:
        无（``paths`` 是无副作用的固定路径模块，可安全顶层导入）。
    """

    from app.utils import paths

    return paths.SYSTEM_COSIR_DIR

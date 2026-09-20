#!/usr/bin/env python3
"""log-triage 的落盘路径推导（与后端 ``app/utils/paths.py`` 同一事实）。

单一职责：把「数据根 → ``.cosir`` 下固定路径」的推导集中在一处，供 ``query_logs.py`` 与
``query_app_db.py`` 共用，避免两个脚本各自手写一份、随后端布局漂移而查错目录。

职责边界：
- 负责：解析数据根（显式环境变量 → 桌面数据根 → 仓库根）、拼接 ``.cosir`` 下的日志目录与
  业务库路径。
- 不负责：读写文件、打开数据库、渲染输出（见各脚本与 ``appdb_*`` 模块）。

设计边界：**不导入 ``app.*``**——本 skill 必须能在后端未启动、启动失败或 UI 打不开时运行，
因此这里复制后端的推导规则而非复用其代码。事实源：``apps/backend/app/utils/paths.py``
（``DATA_DIR`` 只认 ``CODING_AGENT_DATA_DIR``，其余路径统一落在 ``<DATA_DIR>/.cosir/`` 下）与
``apps/desktop/src-tauri/tauri.conf.json`` 的 ``identifier``；三者任一变更都必须同步本文件。
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

COSIR_DIR_NAME = ".cosir"
LOG_DIR_NAME = "logs"
STORAGE_DIR_NAME = "storage"
APP_DB_FILE_NAME = "app.sqlite3"

# Tauri 的 bundle identifier（apps/desktop/src-tauri/tauri.conf.json），决定 app_data_dir()
# 的目录名，也就是「桌面态数据根」。桌面宿主无条件把该目录注入 CODING_AGENT_DATA_DIR。
_DESKTOP_BUNDLE_ID = "com.cosir.desktop"


def repository_root() -> Path:
    """向上查找包含 ``apps/backend`` 的仓库根目录。

    查找顺序：先按**脚本所在目录**向上找，再按**当前工作目录**向上找。后者用于 skill 被
    安装到用户级目录（如 ``~/.codebuddy/skills/``）后仍从仓库根调用的场景——此时仅靠脚本
    路径永远找不到仓库。

    参数:
        无。

    返回:
        包含 ``apps/backend`` 的仓库根绝对路径。

    异常:
        ValueError: 两条路径向上都找不到仓库根；调用方应改用 ``--db`` / ``--log-file`` 显式指定。

    副作用:
        解析当前脚本路径与当前工作目录。
    """

    for start in (Path(__file__).resolve().parent, Path.cwd()):
        for candidate in (start, *start.parents):
            if (candidate / "apps" / "backend").exists():
                return candidate
    raise ValueError(
        "cannot locate repository root (no 'apps/backend' found from the skill path or the "
        "current working directory); pass --db / --log-file explicitly"
    )


def desktop_data_root() -> Path:
    """返回平台默认的桌面应用数据根（等价于 Tauri ``app_data_dir()``）。

    参数:
        无。

    返回:
        Windows 为 ``%APPDATA%\\com.cosir.desktop``（Roaming），macOS 为
        ``~/Library/Application Support/com.cosir.desktop``，其余平台为
        ``$XDG_DATA_HOME``（缺省 ``~/.local/share``）下的同名目录。

    异常:
        ValueError: Windows 上 ``APPDATA`` 未设置时抛出。

    副作用:
        读取 ``APPDATA`` / ``XDG_DATA_HOME`` 环境变量。
    """

    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "").strip()
        if not base:
            raise ValueError("APPDATA is not set; cannot locate the desktop data root")
        return Path(base) / _DESKTOP_BUNDLE_ID
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _DESKTOP_BUNDLE_ID
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / _DESKTOP_BUNDLE_ID


def data_root() -> Path:
    """解析数据根，顺序与后端一致：显式环境变量 → 已存在的桌面数据根 → 仓库根。

    与后端的差异：后端只认 ``CODING_AGENT_DATA_DIR``，缺失即回落仓库根；而排查脚本在两个
    位置**都可能有数据**（桌面应用与直跑后端各自维护一份 ``.cosir``），因此这里优先选择
    「已经存在 ``.cosir`` 子树」的那一个——桌面应用在使用时，排查者要看的通常是它的数据。

    参数:
        无。

    返回:
        数据根绝对路径；``<数据根>/.cosir/`` 是日志、业务库与 runtime 的统一父目录。

    异常:
        ValueError: 无显式变量、桌面数据根不含 ``.cosir``、且仓库根也不可定位时抛出。

    副作用:
        读取 ``CODING_AGENT_DATA_DIR`` 环境变量，并探测两处 ``.cosir`` 目录是否存在。
    """

    return data_root_with_reason()[0]


def data_root_with_reason() -> tuple[Path, str]:
    """解析数据根并同时给出「来源说明」。

    判定与文案共用这一处实现，避免提示层再推导一遍而产生第二套漂移源。回退顺序：
    ``CODING_AGENT_DATA_DIR`` → **已存在 ``.cosir`` 的桌面数据根** → 仓库根。

    参数:
        无。

    返回:
        ``(数据根, 来源说明)``；来源为 ``from CODING_AGENT_DATA_DIR``、
        ``desktop app_data_dir (.cosir present)``、``repository root``（仓库根含 ``.cosir``）
        或 ``repository root, fallback: no .cosir found``（两处都没有 ``.cosir``，仅兜底）。

    异常:
        ValueError: 无显式变量、桌面数据根不含 ``.cosir``、且仓库根也不可定位时抛出。

    副作用:
        读取 ``CODING_AGENT_DATA_DIR`` 环境变量并探测 ``.cosir`` 是否存在。
    """

    explicit = os.environ.get("CODING_AGENT_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit), "from CODING_AGENT_DATA_DIR"

    # 桌面数据根必须「已有 .cosir」才算命中；缺 APPDATA 或目录不存在都不阻断后续回退。
    # 仓库根故意惰性求值：skill 装在用户级目录且 cwd 不在仓库内时它会抛错，但那种场景
    # 只要桌面数据根可用就应当正常工作。
    with contextlib.suppress(ValueError):
        desktop = desktop_data_root()
        if (desktop / COSIR_DIR_NAME).is_dir():
            return desktop, "desktop app_data_dir (.cosir present)"

    try:
        repository = repository_root()
    except ValueError as exc:
        raise ValueError(
            "cannot locate the data root: CODING_AGENT_DATA_DIR is unset, the desktop data root "
            "has no .cosir, and no repository root could be located; pass --db / --log-file "
            "explicitly"
        ) from exc
    if (repository / COSIR_DIR_NAME).is_dir():
        return repository, "repository root"
    return repository, "repository root, fallback: no .cosir found"


def system_cosir_dir(root: Path | None = None) -> Path:
    """返回系统级 ``.cosir`` 目录。

    参数:
        root: 数据根；省略时按 :func:`data_root` 推导。

    返回:
        ``<数据根>/.cosir`` 路径（不保证存在）。

    异常:
        ValueError: 数据根无法推导时抛出。

    副作用:
        无（省略 ``root`` 时读取环境变量与文件系统）。
    """

    return (root if root is not None else data_root()) / COSIR_DIR_NAME


def log_dir(root: Path | None = None) -> Path:
    """返回后端固定 JSONL 日志目录。

    参数:
        root: 数据根；省略时按 :func:`data_root` 推导。

    返回:
        ``<数据根>/.cosir/logs`` 路径（不保证存在）。

    异常:
        ValueError: 数据根无法推导时抛出。

    副作用:
        无（省略 ``root`` 时读取环境变量与文件系统）。
    """

    return system_cosir_dir(root) / LOG_DIR_NAME


def app_db_path(root: Path | None = None) -> Path:
    """返回业务库 SQLite 文件路径。

    参数:
        root: 数据根；省略时按 :func:`data_root` 推导。

    返回:
        ``<数据根>/.cosir/storage/app.sqlite3`` 路径（不保证存在）。

    异常:
        ValueError: 数据根无法推导时抛出。

    副作用:
        无（省略 ``root`` 时读取环境变量与文件系统）。
    """

    return system_cosir_dir(root) / STORAGE_DIR_NAME / APP_DB_FILE_NAME


def describe_path_choice() -> str:
    """返回一行人类可读的数据根说明，供脚本提示「正在查哪一份数据」。

    参数:
        无。

    返回:
        形如 ``data root: <path> (reason)`` 的单行说明；数据根不可推导时返回
        ``data root: <unresolved> (...原因...)``，由调用方原样打印以保留失败线索。

    异常:
        无（推导失败被转成文本，提示属诊断信息，不应中断查询）。

    副作用:
        读取环境变量并探测文件系统。
    """

    try:
        root, reason = data_root_with_reason()
    except ValueError as exc:
        return f"data root: <unresolved> ({exc})"
    return f"data root: {root} ({reason})"

"""进程固定路径常量。

本模块是后端进程固定路径的唯一事实源。桌面宿主把系统级数据根
``app_data_dir()`` 通过 ``CODING_AGENT_DATA_DIR`` 注入；后端再把所有运行期数据统一放入
该根目录下的 ``.cosir`` 子目录。直接运行后端时，数据根回落到仓库根目录，便于开发和测试。

路径布局：

``<DATA_DIR>/.cosir/.env``
``<DATA_DIR>/.cosir/logs/``
``<DATA_DIR>/.cosir/storage/app.sqlite3``
``<DATA_DIR>/.cosir/storage/langgraph_checkpoints.sqlite``
``<DATA_DIR>/.cosir/runtime/``

本模块只做路径推导，不创建目录、不读写文件。目录创建由 Tauri 宿主、日志配置和存储初始化
各自负责；``Settings.load`` 在加载系统 ``.env`` 后调用 ``reset``，使环境变量与固定路径重新对齐。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from app.utils.cosir_paths import COSIR_DIR_NAME

_STORAGE_DIR_NAME: Final[str] = "storage"
_LOG_DIR_NAME: Final[str] = "logs"
_RUNTIME_DIR_NAME: Final[str] = "runtime"
_DATABASE_FILE_NAME: Final[str] = "app.sqlite3"
_CHECKPOINT_FILE_NAME: Final[str] = "langgraph_checkpoints.sqlite"


def repository_root() -> Path:
    """返回仓库根目录绝对路径。"""

    return Path(__file__).resolve().parents[4]


def _env_path(name: str) -> Path | None:
    """读取并规范化路径环境变量。

    空串和纯空白按未设置处理；其余值仅去除首尾空白，不负责校验存在性或创建目录。
    """

    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return Path(value) if value else None


def _resolve_data_dir() -> Path:
    """解析系统应用数据根。

    桌面模式使用 Tauri 注入的 ``CODING_AGENT_DATA_DIR``；直接运行后端时使用仓库根目录。
    ``.cosir`` 子目录由本模块统一追加，不允许日志、数据库或 checkpoint 各自选择根目录。
    """

    return _env_path("CODING_AGENT_DATA_DIR") or repository_root()


def _data_dir(data_dir: Path) -> Path:
    """返回系统应用数据根。"""

    return data_dir


def _system_cosir_dir(data_dir: Path) -> Path:
    """返回系统级 ``.cosir`` 目录。"""

    return data_dir / COSIR_DIR_NAME


def _log_dir(data_dir: Path) -> Path:
    """返回系统级 JSONL 日志目录。"""

    return _system_cosir_dir(data_dir) / _LOG_DIR_NAME


def _database_file(data_dir: Path) -> Path:
    """返回系统级主业务 SQLite 文件。"""

    return _system_cosir_dir(data_dir) / _STORAGE_DIR_NAME / _DATABASE_FILE_NAME


def _checkpoint_file(data_dir: Path) -> Path:
    """返回系统级 LangGraph checkpoint SQLite 文件。"""

    return _system_cosir_dir(data_dir) / _STORAGE_DIR_NAME / _CHECKPOINT_FILE_NAME


def _runtime_dir(data_dir: Path) -> Path:
    """返回系统级运行时目录。"""

    return _system_cosir_dir(data_dir) / _RUNTIME_DIR_NAME


_DERIVERS: Final[dict[str, Callable[[Path], Path]]] = {
    "DATA_DIR": _data_dir,
    "SYSTEM_COSIR_DIR": _system_cosir_dir,
    "LOG_DIR": _log_dir,
    "DATABASE_FILE": _database_file,
    "CHECKPOINT_FILE": _checkpoint_file,
    "RUNTIME_DIR": _runtime_dir,
}


def _derive(data_dir: Path) -> dict[str, Path]:
    """按同一个数据根推导全部固定路径。"""

    return {name: derive(data_dir) for name, derive in _DERIVERS.items()}


_initial = _derive(_resolve_data_dir())
DATA_DIR: Path = _initial["DATA_DIR"]
SYSTEM_COSIR_DIR: Path = _initial["SYSTEM_COSIR_DIR"]
LOG_DIR: Path = _initial["LOG_DIR"]
DATABASE_FILE: Path = _initial["DATABASE_FILE"]
CHECKPOINT_FILE: Path = _initial["CHECKPOINT_FILE"]
RUNTIME_DIR: Path = _initial["RUNTIME_DIR"]


def env_files() -> tuple[Path, Path]:
    """返回系统级运行配置文件路径，顺序为基础配置和本地覆盖配置。"""

    return SYSTEM_COSIR_DIR / ".env", SYSTEM_COSIR_DIR / ".env.local"


def reset() -> None:
    """按当前进程环境重新计算全部固定路径常量。"""

    globals().update(_derive(_resolve_data_dir()))


def override(**kwargs: Any) -> None:
    """为测试覆盖固定路径。

    传入 ``DATA_DIR`` 时会先按新的数据根重算所有派生路径，再应用其它显式覆盖值；不传入
    ``DATA_DIR`` 时只覆盖指定路径。生产代码不得调用，测试应在收尾调用 ``reset``。
    """

    invalid = set(kwargs) - set(_DERIVERS)
    if invalid:
        raise ValueError(f"unknown paths to override: {sorted(invalid)}")
    for name, value in kwargs.items():
        if not isinstance(value, Path):
            raise TypeError(f"{name} must be a pathlib.Path, got {type(value).__name__}")

    if "DATA_DIR" in kwargs:
        globals().update(_derive(kwargs["DATA_DIR"]))
    globals().update(kwargs)


__all__ = [*_DERIVERS, "override", "repository_root", "reset"]

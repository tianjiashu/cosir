"""系统级 ``.cosir`` 路径与进程固定路径常量（唯一事实源）。

桌面宿主把系统级数据根通过 ``CODING_AGENT_DATA_DIR`` 注入；macOS/Windows 使用用户主目录，
后端再把所有运行期数据统一放入该根目录下的 ``.cosir`` 子目录。直接运行后端时，macOS/Windows
同样使用用户主目录，其他平台必须由桌面宿主注入 ``CODING_AGENT_DATA_DIR``，否则启动期路径解析失败。

路径布局：

``<DATA_DIR>/.cosir/.env``
``<DATA_DIR>/.cosir/logs/``
``<DATA_DIR>/.cosir/storage/app.sqlite3``
``<DATA_DIR>/.cosir/storage/langgraph_checkpoints.sqlite``
``<DATA_DIR>/.cosir/runtime/``
``<DATA_DIR>/.cosir/config/terminal_denylist.json``

本模块只做路径推导，不创建目录、不读写文件。目录创建由 Tauri 宿主、日志配置和存储初始化
各自负责；``Settings.load`` 在加载系统 ``.env`` 后调用 ``reset``，使环境变量与固定路径重新对齐。

``.cosir`` 基名由 ``app.utils.path.workspace_cosir.COSIR_DIR_NAME`` 固定，不作为配置项。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from app.utils.path.workspace_cosir import (
    COSIR_AGENT_CONFIG_DIR_NAME,
    COSIR_AGENT_TEAM_CONFIG_DIR_NAME,
    COSIR_DIR_NAME,
    COSIR_ENV_FILE_NAME,
    COSIR_INSTRUCTION_FILE_NAME,
    COSIR_MAIN_AGENT_PROMPT_FILE_NAME,
)

_STORAGE_DIR_NAME: Final[str] = "storage"
_LOG_DIR_NAME: Final[str] = "logs"
_RUNTIME_DIR_NAME: Final[str] = "runtime"
_CONFIG_DIR_NAME: Final[str] = "config"
_TERMINAL_DENYLIST_FILE_NAME: Final[str] = "terminal_denylist.json"
_DATABASE_FILE_NAME: Final[str] = "app.sqlite3"
_CHECKPOINT_FILE_NAME: Final[str] = "langgraph_checkpoints.sqlite"
LOG_FILE_NAME: Final[str] = "backend.log"


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

    桌面模式使用 Tauri 注入的 ``CODING_AGENT_DATA_DIR``；直接运行后端时，macOS 与
    Windows 使用当前用户主目录。其他平台不支持未注入数据根的运行方式：必须由桌面宿主注入
    ``CODING_AGENT_DATA_DIR``，否则抛出异常。``.cosir`` 子目录由本模块统一追加，不允许
    日志、数据库或 checkpoint 各自选择根目录。
    """

    configured = _env_path("CODING_AGENT_DATA_DIR")
    if configured is not None:
        return configured
    if sys.platform in {"darwin", "win32"}:
        return Path.home()
    raise RuntimeError(
        "未设置 CODING_AGENT_DATA_DIR 且当前平台不是 macOS/Windows；"
        "请由桌面宿主注入 CODING_AGENT_DATA_DIR，或在受支持的平台运行后端。"
    )


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


def system_cosir_dir() -> Path:
    """返回系统级 ``.cosir`` 目录路径（``<数据根>/.cosir``）。"""

    return SYSTEM_COSIR_DIR


def system_instruction_file() -> Path:
    """返回系统级全局指令文件路径（``<system_cosir_dir>/AGENTS.md``）。"""

    return system_cosir_dir() / COSIR_INSTRUCTION_FILE_NAME


def system_main_agent_prompt_file() -> Path:
    """返回系统级主 Agent 系统提示词配置文件路径。"""

    return system_cosir_dir() / COSIR_MAIN_AGENT_PROMPT_FILE_NAME


def system_agent_config_dir() -> Path:
    """返回系统级子 Agent JSON 配置目录路径（``<system_cosir_dir>/agents``）。"""

    return system_cosir_dir() / COSIR_AGENT_CONFIG_DIR_NAME


def system_agent_team_config_dir() -> Path:
    """返回系统级 Team JSON 配置目录路径。"""

    return system_cosir_dir() / COSIR_AGENT_TEAM_CONFIG_DIR_NAME


def system_env_file() -> Path:
    """返回系统级基础环境配置文件路径（``<system_cosir_dir>/.env``）。"""

    return system_cosir_dir() / COSIR_ENV_FILE_NAME


def system_terminal_denylist_file() -> Path:
    """返回系统级终端 deny-list 配置文件路径。"""

    return system_cosir_dir() / _CONFIG_DIR_NAME / _TERMINAL_DENYLIST_FILE_NAME


def env_file() -> Path:
    """返回系统级运行配置文件路径（``<数据根>/.cosir/.env``）。"""

    return system_env_file()


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


__all__ = [
    *list(_DERIVERS),
    "override",
    "reset",
    "env_file",
    "LOG_FILE_NAME",
    "system_cosir_dir",
    "system_instruction_file",
    "system_main_agent_prompt_file",
    "system_agent_config_dir",
    "system_agent_team_config_dir",
    "system_env_file",
    "COSIR_DIR_NAME",
    "COSIR_AGENT_CONFIG_DIR_NAME",
    "COSIR_AGENT_TEAM_CONFIG_DIR_NAME",
    "COSIR_INSTRUCTION_FILE_NAME",
    "COSIR_MAIN_AGENT_PROMPT_FILE_NAME",
    "COSIR_ENV_FILE_NAME",
]

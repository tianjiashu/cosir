"""配置文件的安全读写：路径包含校验、路径锁、原子替换，以及配置 JSON 的统一读写格式。

本模块只做文件层面的事，不解释 Agent、Markdown 或 env 的业务含义；调用方先完成业务校验再写入。
写入目标必须由后端根据固定路径函数与已校验标识生成：路径检查只阻止词法路径逃出给定根目录，不探测
本机配置目录的符号链接或文件类型。全部是无状态函数，模块本身就是命名空间。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.config.logging.logger import log

_LOCK_REGISTRY_GUARD = Lock()
_PATH_LOCKS: dict[Path, RLock] = {}

ConfigModel = TypeVar("ConfigModel", bound=BaseModel)


class ConfigurationFileError(OSError):
    """配置文件无法读取或原子写入。"""


class ConfigurationPathError(ValueError):
    """配置目标不在受信任目录内。"""


def assert_safe_child(root: Path, candidate: Path) -> Path:
    """校验并返回词法上位于 ``root`` 下的配置路径。"""

    root = Path(root)
    candidate = Path(candidate)
    if not root.is_absolute() or not candidate.is_absolute():
        raise ConfigurationPathError("configuration path must be absolute")
    root = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(candidate))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigurationPathError(
            f"configuration path escapes trusted root: {candidate}"
        ) from exc
    if candidate == root:
        raise ConfigurationPathError("configuration target must be a child file")
    return candidate


def read_text(path: Path, *, root: Path) -> str:
    """校验路径在 ``root`` 之内后读取 UTF-8 配置文件。"""

    target = assert_safe_child(root, path)
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.error(
            "configuration_read_failed",
            extra={
                "msg": "配置文件读取失败",
                "data": {"path": str(target), "error_type": type(exc).__name__},
            },
        )
        raise ConfigurationFileError(f"configuration read failed: {target}") from exc


def _path_lock(path: Path) -> RLock:
    target = path.absolute()
    with _LOCK_REGISTRY_GUARD:
        lock = _PATH_LOCKS.get(target)
        if lock is None:
            lock = RLock()
            _PATH_LOCKS[target] = lock
        return lock


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """锁定一个配置路径，供调用方把读、校验、写/删组成不可交错的临界区。"""

    with _path_lock(path):
        yield


def write_text_atomic(path: Path, content: str, *, root: Path) -> None:
    """在路径锁内以受限权限原子替换文本文件。

    锁是可重入的：调用方要把「读、校验、写」组成临界区时在外层持 :func:`locked`，只写一次时
    不必再包一层。
    """

    target = assert_safe_child(root, path)
    with _path_lock(path):
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary_path = Path(raw_path)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                os.chmod(temporary_path, stat.S_IRUSR | stat.S_IWUSR)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                # 内容替换已经成功，不回滚文件；将目录持久化间隙写入后端落盘日志，便于排查。
                log.warning(
                    "configuration_directory_fsync_failed",
                    extra={
                        "msg": "配置文件已替换，但目录同步失败",
                        "data": {"directory": str(target.parent), "error_type": type(exc).__name__},
                    },
                )
        except (OSError, UnicodeError) as exc:
            log.error(
                "configuration_write_failed",
                extra={
                    "msg": "配置文件原子写入失败",
                    "data": {"path": str(target), "error_type": type(exc).__name__},
                },
            )
            raise ConfigurationFileError(f"configuration write failed: {target}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def delete_file(path: Path, *, root: Path) -> None:
    """在路径锁内删除配置文件；文件是否存在由调用方在同一临界区里先判定。"""

    target = assert_safe_child(root, path)
    with _path_lock(path):
        try:
            target.unlink()
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            log.error(
                "configuration_delete_failed",
                extra={
                    "msg": "配置文件删除失败",
                    "data": {"path": str(target), "error_type": type(exc).__name__},
                },
            )
            raise ConfigurationFileError(f"configuration delete failed: {target}") from exc


def write_json(path: Path, document: BaseModel | dict[str, Any], *, root: Path) -> None:
    """按配置 JSON 的统一格式原子写入：缩进 2、保留中文、末尾换行，所有配置文件看起来一致。"""

    data = document.model_dump(mode="json") if isinstance(document, BaseModel) else document
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", root=root)


def read_json_model(
    path: Path,
    model: type[ConfigModel],
    *,
    error: type[ValueError],
    label: str,
    event: str,
    log_data: dict[str, Any],
) -> ConfigModel:
    """读取并校验一个配置 JSON；文件不存在时按模型默认值处理。

    读不了或格式不对都记一条错误日志，再抛 ``error``：这类错误是配置内容问题（接入层映射为 400），
    与写入失败的 ``ConfigurationFileError``（500）分开。``label`` 是给人看的配置名（如「Skill 配置」），
    ``event`` 是日志事件前缀（``<event>_read_failed`` / ``<event>_invalid``）。
    """

    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return model()
    except (OSError, UnicodeDecodeError) as exc:
        log.error(
            f"{event}_read_failed",
            extra={
                "msg": f"读取 {label}失败",
                "data": {**log_data, "error_type": type(exc).__name__},
            },
        )
        raise error(f"{label}文件无法读取") from exc
    try:
        return model.model_validate_json(content)
    except ValidationError as exc:
        log.error(
            f"{event}_invalid",
            extra={
                "msg": f"{label}文件格式无效",
                "data": {**log_data, "error_type": type(exc).__name__},
            },
        )
        raise error(f"{label}文件格式无效{_invariant_reason(exc)}") from exc


def _invariant_reason(exc: ValidationError) -> str:
    """模型自己声明的不变量（如「服务名称必须唯一」）原样告诉用户；字段类型等结构错误只说格式无效。"""

    for item in exc.errors():
        if item["type"] == "value_error":
            return f"：{str(item['msg']).removeprefix('Value error, ')}"
    return ""

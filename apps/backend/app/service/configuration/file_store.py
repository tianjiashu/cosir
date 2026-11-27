"""配置文件的安全读写：路径包含校验、路径锁、原子替换、内容版本前置条件，以及配置 JSON 的统一读写格式。

本模块只做文件层面的事，不解释 Agent、Markdown 或 env 的业务含义；调用方先完成业务校验再写入。
写入目标必须由后端根据固定路径函数与已校验标识生成：路径检查只阻止词法路径逃出给定根目录，不探测
本机配置目录的符号链接或文件类型。全部是无状态函数，模块本身就是命名空间。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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


class ConfigurationRevisionConflict(Exception):
    """整份替换的写入前置条件不成立：文件在读取之后被别处改过（手改、git、Agent 写文件）。

    ``current_revision`` 是锁内读到的当前版本（文件不存在为 ``None``），接入层原样交给前端，用户选择
    「覆盖」时以它为基准再写一次。不继承 ``ValueError`` / ``OSError``：它不是内容错误也不是读写失败。
    """

    def __init__(self, current_revision: str | None) -> None:
        super().__init__("配置文件已在别处修改，保存未生效")
        self.current_revision = current_revision


@dataclass(frozen=True)
class RevisedText:
    """一次读取得到的正文与它的内容版本（同一份字节算出，二者不会错位）。"""

    content: str
    revision: str


def text_revision(content: str) -> str:
    """一段即将写入的正文落盘后的版本；与 :func:`file_revision` 对同一份字节算出的值相同。"""

    return _bytes_revision(content.encode("utf-8"))


def _bytes_revision(raw: bytes) -> str:
    # 版本只认内容：mtime 精度不够、git 会保留时间戳；外部写回相同内容也不算冲突。
    return hashlib.sha256(raw).hexdigest()


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


def read_revised_text(path: Path, *, root: Path) -> RevisedText:
    """读取 UTF-8 配置文件，连同内容版本一起返回；换行按通用换行规则归一为 ``\\n``（与 :func:`read_text` 一致）。"""

    target = assert_safe_child(root, path)
    try:
        raw = target.read_bytes()
        content = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError) as exc:
        log.error(
            "configuration_read_failed",
            extra={
                "msg": "配置文件读取失败",
                "data": {"path": str(target), "error_type": type(exc).__name__},
            },
        )
        raise ConfigurationFileError(f"configuration read failed: {target}") from exc
    return RevisedText(content=content, revision=_bytes_revision(raw))


def file_revision(path: Path, *, root: Path) -> str | None:
    """文件当前的内容版本；不存在时为 ``None``（「期望不存在」也是一种前置条件）。"""

    target = assert_safe_child(root, path)
    try:
        return _bytes_revision(target.read_bytes())
    except FileNotFoundError:
        return None
    except OSError as exc:
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


def write_text_if_revision(
    path: Path, content: str, *, root: Path, base_revision: str | None
) -> str:
    """整份替换的写入：锁内比对当前版本与 ``base_revision``，一致才原子写入，返回新版本。

    路径锁只挡本进程的写入方；外部写入者（编辑器、git）不拿这把锁，比对到替换之间只剩极短的
    时间窗，按「低概率事件在一处边界兜住」接受。

    异常:
        ConfigurationRevisionConflict: 当前版本与 ``base_revision`` 不一致。
        ConfigurationFileError: 读取当前版本或写入失败。
    """

    with _path_lock(path):
        current = file_revision(path, root=root)
        if current != base_revision:
            log.warning(
                "configuration_revision_conflict",
                extra={
                    "msg": "配置文件已在别处修改，拒绝覆盖",
                    "data": {
                        "path": str(path),
                        "base_revision": base_revision,
                        "current_revision": current,
                    },
                },
            )
            raise ConfigurationRevisionConflict(current)
        write_text_atomic(path, content, root=root)
    return text_revision(content)


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

    write_text_atomic(path, json_text(document), root=root)


def json_text(document: BaseModel | dict[str, Any]) -> str:
    """配置 JSON 的统一落盘文本；需要带版本前置条件写入时交给 :func:`write_text_if_revision`。"""

    data = document.model_dump(mode="json") if isinstance(document, BaseModel) else document
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


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

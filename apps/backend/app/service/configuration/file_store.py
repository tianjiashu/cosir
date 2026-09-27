"""系统配置文件安全读写基础设施。

本模块只负责配置文件的路径安全检查、锁定和原子文本替换，不解释 Agent、Markdown 或 env
内容。调用方必须先完成业务校验，再调用 :class:`ConfigurationFileStore` 写入。
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock

from app.config.logging.logger import log

_LOCK_REGISTRY_GUARD = Lock()
_PATH_LOCKS: dict[Path, RLock] = {}


class ConfigurationFileError(OSError):
    """配置文件无法读取或原子写入。"""


class ConfigurationPathError(ValueError):
    """配置目标不在受信任目录内或命中了符号链接安全边界。"""


class ConfigurationFileStore:
    """为系统 `.cosir` 配置提供受限路径和原子文本文件操作。

    本类不创建业务目录、不解析内容、不维护缓存。写入目标必须由后端根据固定路径函数生成；
    `assert_safe_child` 只允许目标位于给定根目录内，并拒绝目录、目标文件和祖先路径中的符号链接。
    """

    @staticmethod
    def assert_safe_child(root: Path, candidate: Path) -> Path:
        """校验并返回位于 ``root`` 下的普通文件候选路径。"""

        root = Path(root)
        candidate = Path(candidate)
        if not candidate.is_absolute():
            raise ConfigurationPathError("configuration path must be absolute")
        try:
            root_resolved = root.resolve(strict=False)
            candidate_resolved = candidate.resolve(strict=False)
            candidate_resolved.relative_to(root_resolved)
        except (OSError, ValueError) as exc:
            raise ConfigurationPathError(
                f"configuration path escapes trusted root: {candidate}"
            ) from exc
        if candidate_resolved == root_resolved:
            raise ConfigurationPathError("configuration target must be a child file")
        if root.exists() and root.is_symlink():
            raise ConfigurationPathError(f"configuration root must not be a symlink: {root}")
        current = candidate
        while current != root:
            if current.is_symlink():
                raise ConfigurationPathError(
                    f"configuration path must not contain symlink: {current}"
                )
            current = current.parent
            if len(current.parts) < len(root.parts):
                raise ConfigurationPathError(
                    f"configuration path escapes trusted root: {candidate}"
                )
        if candidate.exists() and (candidate.is_symlink() or not candidate.is_file()):
            raise ConfigurationPathError(
                f"configuration target must be a regular file: {candidate}"
            )
        return candidate

    @classmethod
    def read_text(cls, path: Path, *, root: Path | None = None) -> str:
        """读取 UTF-8 配置文件；存在安全根时先执行路径校验。"""

        target = cls.assert_safe_child(root, path) if root is not None else Path(path)
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

    @staticmethod
    def _path_lock(path: Path) -> RLock:
        target = path.absolute()
        with _LOCK_REGISTRY_GUARD:
            lock = _PATH_LOCKS.get(target)
            if lock is None:
                lock = RLock()
                _PATH_LOCKS[target] = lock
            return lock

    @classmethod
    @contextmanager
    def locked(cls, path: Path) -> Iterator[None]:
        """锁定一个配置路径，供调用方把读、校验、写/删组成不可交错的临界区。"""

        with cls._path_lock(path):
            yield

    @classmethod
    def write_text_atomic(
        cls,
        path: Path,
        content: str,
        *,
        root: Path | None = None,
    ) -> None:
        """串行执行受限路径下的原子文本写入。"""

        with cls._path_lock(path):
            return cls._write_text_atomic(
                path,
                content,
                root=root,
            )

    @classmethod
    def _write_text_atomic(
        cls,
        path: Path,
        content: str,
        *,
        root: Path | None = None,
    ) -> None:
        """以受限权限原子替换文本文件，不生成或校验并发元数据。"""

        target = cls.assert_safe_child(root, path) if root is not None else Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_symlink():
            raise ConfigurationPathError(f"configuration target must not be a symlink: {target}")
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
    @classmethod
    def delete_file(
        cls,
        path: Path,
        *,
        root: Path,
    ) -> None:
        """串行执行受限路径下的配置文件删除。"""

        with cls._path_lock(path):
            cls._delete_file(path, root=root)

    @classmethod
    def _delete_file(
        cls,
        path: Path,
        *,
        root: Path,
    ) -> None:
        """删除受限配置文件。"""

        target = cls.assert_safe_child(root, path)
        if not target.exists():
            raise FileNotFoundError(target)
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

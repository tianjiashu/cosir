"""项目路径安全解析器（PathResolver）。

把 ``read_file`` 原先内联的私有路径安全逻辑提取为独立抽象，供文件工具
共用（符合 Rule of Three，避免路径安全逻辑在多个文件工具间重复）。

设计边界：
- 只解析与拦截路径，不读不写文件。
- 三个入口，均返回 ``(Path | None, str)`` 二元组，错误为面向模型的英文短句：
  - ``resolve_for_write``：写/改等破坏性工具。相对路径按工作区根解析，目标必须落在可写根内
    （未收窄的 Session 即工作区根，见 ``core.permission.writable_root``），拒绝 workspace
    ``.cosir`` 保留区与受保护路径，没有放行开关。
  - ``resolve_for_read``：读一份文件（``read_file`` / ``read_image`` / 浏览器上传）。不强制
    containment，解析前后各拦一次设备路径，拒绝禁读的受保护路径。
  - ``resolve_read_scope``：读一个范围（``search_content`` /
    ``find_files``，可以是目录也可以是文件）。在读一份文件的规则上再拒绝 ``/proc`` 等伪文件树；
    从上层目录遍历时跳过受保护子树由搜索遍历负责（``search/file_walker``）。
- 受保护路径的事实源是 ``app.core.permission.protected_paths``；读写两侧只在这三个入口消费。
- ``blocked_device_reason`` 返回空串表示允许；写类工具自行在解析前后各调一次。
- 后端自行拼出的内部落盘路径（工具产物）不经本解析器，越界防护由 ``atomic_write_text`` 的
  ``containment_root`` 承担。
"""

from pathlib import Path
from typing import ClassVar

from app.config.logging.logger import log
from app.core.permission.protected_paths import ProtectedPaths
from app.utils.path.relative_path import has_nul
from app.utils.path.validation import is_within, is_within_cosir


class PathResolver:
    """将用户传入的路径解析到项目根目录内的安全解析器。

    单一职责：解析路径并拦截设备路径与受保护路径，按写、读一份文件、读一个范围三种入口
    提供（见模块说明）。所有文件工具共用同一套拦截与解析规则，避免规则漂移。

    参数:
        project_root: 允许访问的项目根目录，所有路径必须解析到其内部。

    返回:
        ``ProjectPathResolver`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存项目根目录路径；不读取、不写入文件。
    """

    windows_device_names: ClassVar[set[str]] = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "COM¹",
        "COM²",
        "COM³",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
        "LPT¹",
        "LPT²",
        "LPT³",
    }
    posix_blocked_device_paths: ClassVar[set[str]] = {
        "/dev/null",
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/full",
        "/dev/stdin",
        "/dev/tty",
        "/dev/console",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
    proc_blocked_suffixes: ClassVar[tuple[str, ...]] = (
        "/fd/0",
        "/fd/1",
        "/fd/2",
        "/environ",
        "/cmdline",
        "/maps",
        "/smaps",
        "/smaps_rollup",
        "/numa_maps",
        "/mem",
        "/auxv",
        "/pagemap",
    )
    posix_blocked_recursive_roots: ClassVar[tuple[str, ...]] = (
        "/proc",
        "/sys",
        "/dev",
    )

    def __init__(self, workspace_root: str | Path, writable_root: str | Path | None = None) -> None:
        """固化工作区根与可写根；不读文件系统。

        ``writable_root`` 只有写入口用，读类工具不传；缺省即工作区根。
        """

        self.workspace_root = Path(workspace_root)
        self.writable_root = self.workspace_root if writable_root is None else Path(writable_root)

    def resolve_for_write(self, path: str) -> tuple[Path | None, str]:
        """解析写入目标：相对路径按工作区根解析，目标须在可写根内，并拒绝保留区与受保护路径。

        返回 ``(resolved_path, "")`` 或 ``(None, error)``，``error`` 为面向模型的英文短句；不向上抛出。
        两边都跟随符号链接后再比较，指向边界外的链接因此被拒。
        """

        input_error = self._validate_path(path)
        if input_error:
            return None, input_error
        try:
            root = self.workspace_root.resolve()
            raw = Path(path)
            resolved = (raw if raw.is_absolute() else root / raw).resolve()
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"path escapes project root: {path} ({exc})"
        if not is_within(resolved, self.writable_root):
            return None, (
                f"path is outside this session's writable root {self.writable_root}: {path}. "
                "This session may only write inside that directory; leave other files alone "
                "and report what needs to change there"
            )
        if is_within_cosir(resolved, self.workspace_root):
            return None, (
                f"path is inside the reserved '.cosir' metadata area: {path}. That area is "
                "read-only; write, edit, delete, and move operations must target files "
                "outside '.cosir'."
            )
        if ProtectedPaths.current().denies_write(resolved):
            return None, self._protected_denial(path, resolved, write=True)
        return resolved, ""

    def _resolve_unbounded(self, path: str) -> tuple[Path | None, str]:
        """解析用户路径但不强制项目根 containment，是两个读入口的共同底座。

        相对路径仍以 workspace 根为基准解析（保持 ``read_file("src/x.py")`` 的体验），workspace
        外的绝对 / 越界路径也允许：读可以出工作区，写不行。返回 ``(None, error)`` 表示空串、
        NUL 或解析异常。
        """

        # 检查用户输入是否合法
        input_error = self._validate_path(path)
        if input_error:
            return None, input_error
        try:
            root = self.workspace_root.resolve()
            raw = Path(path)
            target = raw if raw.is_absolute() else root / raw
            resolved = target.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"cannot resolve path: {path} ({exc})"
        return resolved, ""

    def resolve_for_read(self, path: str) -> tuple[Path | None, str]:
        """读一份文件的路径规则：先按原始字符串拦设备路径（不碰文件系统），解析后再按真实
        目标（跟随符号链接）拦一次设备路径与禁读的受保护路径。读取文件内容的工具都走这里，
        规则只此一份。
        """

        device_error = self.blocked_device_reason(path)
        if device_error:
            return None, device_error
        resolved, error = self._resolve_unbounded(path)
        if resolved is None:
            return None, error
        device_error = self.blocked_device_reason(path, resolved)
        if device_error:
            return None, device_error
        if ProtectedPaths.current().denies_read(resolved):
            return None, self._protected_denial(path, resolved, write=False)
        return resolved, ""

    def resolve_read_scope(self, path: str) -> tuple[Path | None, str]:
        """读一个范围（目录或文件）的路径规则：读一份文件的规则，再拒绝 ``/proc`` 等伪文件树。

        列目录与递归搜索共用；不检查存在性与类型（各工具给出各自的提示）。范围内部的受保护
        子树由搜索遍历剪掉，这里只拦起点本身。
        """

        resolved, error = self.resolve_for_read(path)
        if resolved is None:
            return None, error
        pseudo_error = self._blocked_pseudo_tree_reason(path, resolved)
        if pseudo_error:
            return None, pseudo_error
        return resolved, ""

    def blocked_device_reason(self, path: str, resolved: Path | None = None) -> str:
        """判断路径是否指向应当禁止读取/写入的系统设备或敏感伪文件。

        参数:
            path: 原始路径字符串。
            resolved: 可选的归一化路径，用于二次检查符号链接解析后的目标。

        返回:
            空字符串表示允许继续；非空字符串表示命中禁止路径。

        异常:
            无。

        副作用:
            无。
        """

        # 第 1 层：检查「用户原始输入字符串」
        if self._is_blocked_posix_path(path) or self._windows_device_name(path):
            return (
                f"blocked device path: '{path}' refers to an OS device or sensitive "
                "pseudo-file (e.g. NUL/CON/COM1 on Windows, /dev/* or /proc/* on POSIX); "
                "use a regular file path inside the project instead"
            )

        # 第 2 层：检查「符号链接解析后的真实目标」
        if resolved is not None and (
            self._is_blocked_posix_path(resolved.as_posix())
            or self._windows_device_name(str(resolved))
        ):
            return (
                f"blocked device path: '{path}' resolves to an OS device or sensitive "
                f"pseudo-file ({resolved}); use a regular file path inside the project instead"
            )
        return ""

    def _blocked_pseudo_tree_reason(self, path: str, resolved: Path) -> str:
        """判断范围起点是否位于 ``/proc``、``/sys``、``/dev`` 伪文件树；空串表示允许。"""

        for candidate in (str(path), resolved.as_posix()):
            normalized = candidate.replace("\\", "/").lower().rstrip("/")
            if any(
                normalized == root or normalized.startswith(f"{root}/")
                for root in self.posix_blocked_recursive_roots
            ):
                return (
                    f"blocked pseudo-filesystem path: {path}; "
                    "use a regular directory inside the project instead"
                )
        return ""

    @staticmethod
    def _protected_denial(path: str, resolved: Path, *, write: bool) -> str:
        """记录受保护路径拒绝并返回面向模型的说明。"""

        log.warning(
            "protected_path_denied",
            extra={
                "msg": "文件工具访问受保护路径被拒绝",
                "data": {"path": path, "resolved": str(resolved), "write": write},
            },
        )
        return ProtectedPaths.current().denial_message(path, write=write)

    def _is_blocked_posix_path(self, path: str) -> bool:
        """判断路径是否命中 POSIX 设备或 Linux procfs 敏感路径。

        参数:
            path: 待检查路径。

        返回:
            True 表示必须拒绝；False 表示未命中该类规则。

        异常:
            无。

        副作用:
            无。
        """

        normalized = str(path).replace("\\", "/").lower().rstrip("/")
        if normalized in self.posix_blocked_device_paths:
            return True
        return normalized.startswith("/proc/") and normalized.endswith(self.proc_blocked_suffixes)

    def _windows_device_name(self, path: str) -> str:
        """返回命中的 Windows 设备名；没有命中时返回空字符串。

        参数:
            path: 待检查路径。

        返回:
            命中的设备名，例如 ``NUL``；未命中时返回空字符串。

        异常:
            无。

        副作用:
            无。
        """

        normalized = str(path).replace("\\", "/")
        for component in normalized.split("/"):
            cleaned = component.rstrip(" .")
            stem = cleaned.split(":", 1)[0].split(".", 1)[0].rstrip(" .").upper()
            if stem in self.windows_device_names:
                return stem
        return ""

    @staticmethod
    def _validate_path(path: object) -> str:
        """返回原始路径输入错误；合法时返回空字符串。

        参数:
            path: 待验证的原始路径值。

        返回:
            空字符串表示合法；否则返回稳定错误说明。

        异常:
            无。

        副作用:
            无。
        """

        if not isinstance(path, str) or not path.strip():
            return "path must be a non-empty string"
        if has_nul(path):
            return "path must not contain NUL characters"
        return ""

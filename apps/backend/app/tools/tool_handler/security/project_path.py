"""项目路径安全解析器（ProjectPathResolver）。

把 ``read_file`` 原先内联的私有路径安全逻辑提取为独立抽象，供 7 个文件工具
共用（符合 Rule of Three，避免路径安全逻辑在 7 处重复）。本类是本次新增抽象，
``read_file`` 在重构后删除其私有方法并委托本类。

设计边界：
- 只解析与拦截路径，不读不写文件。
- 方法体对齐 ``ReadFileTool`` 真实实现，返回类型与其保持一致：
  ``resolve`` 成功返回 ``(resolved, "")``，失败返回 ``(None, 错误原因)``；
  ``blocked_device_reason`` 返回空串表示允许。
"""

from pathlib import Path
from typing import ClassVar


class ProjectPathResolver:
    """将用户传入的路径解析到项目根目录内的安全解析器。

    单一职责：解析路径并拦截 Windows 设备名与 POSIX 敏感设备/伪文件路径，并提供
    两种作用域策略——:meth:`resolve` 强制项目根 containment（供 write / delete /
    patch 等破坏性工具，越界即拒绝）；:meth:`resolve_unrestricted` 不强制 containment
    （供 read / list 等只读工具，允许访问项目根外）。所有文件工具共用同一套设备
    拦截与解析规则，避免规则漂移。

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

    def __init__(self, workspace_root: str | Path) -> None:
        """初始化解析器并固化项目根目录。

        参数:
            workspace_root: 解析器允许访问的项目根目录。

        返回:
            无。

        异常:
            无。

        副作用:
            仅保存 ``workspace_root``，不执行文件系统读取。
        """

        self.workspace_root = Path(workspace_root)

    def resolve(self, path: str) -> tuple[Path | None, str]:
        """解析用户路径，并确认最终路径仍在项目根目录内。

        参数:
            path: 模型传入的路径字符串。

        返回:
            ``(resolved_path, "")`` 表示成功；``(None, error)`` 表示路径非法。

        异常:
            不向上抛出。解析失败会被转换成错误字符串。

        副作用:
            无。
        """

        input_error = self._path_input_error(path)
        if input_error:
            return None, input_error
        try:
            root = self.workspace_root.resolve()
            raw = Path(path)
            target = raw if raw.is_absolute() else root / raw
            resolved = target.resolve()
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"path escapes project root: {path} ({exc})"
        return resolved, ""

    def resolve_entry(self, path: str) -> tuple[Path | None, str]:
        """解析目录项自身，并确认其父目录仍位于项目根内。

        与 :meth:`resolve` 不同，本方法不会跟随最后一级符号链接。它只用于删除
        符号链接本身等必须区分“目录项”和“链接目标”的场景；中间目录仍会解析，
        因而无法借助父级符号链接逃逸项目根。

        参数:
            path: 模型传入的路径字符串。

        返回:
            ``(entry_path, "")`` 表示成功；``(None, error)`` 表示路径非法或越界。

        异常:
            不向上抛出。解析失败会被转换成错误字符串。

        副作用:
            无。
        """

        input_error = self._path_input_error(path)
        if input_error:
            return None, input_error
        try:
            root = self.workspace_root.resolve()
            raw = Path(path)
            target = raw if raw.is_absolute() else root / raw
            parent = target.parent.resolve()
            entry = parent / target.name
            entry.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"path escapes project root: {path} ({exc})"
        return entry, ""

    def resolve_unrestricted(self, path: str) -> tuple[Path | None, str]:
        """解析用户路径但不强制项目根 containment（供只读工具使用）。

        与 :meth:`resolve` 的唯一区别是省略项目根 containment 校验：相对路径仍以
        项目根为基准解析（保持既有 UX，``read_file("src/x.py")`` 照常），但解析到
        项目根外的绝对/越界路径也被允许。仅供非破坏性的 read / list 类工具使用；
        write / delete / patch 必须继续用 :meth:`resolve` 强制 workspace 边界。
        设备/伪文件拦截仍由 :meth:`blocked_device_reason` 独立负责，不在本方法内。

        参数:
            path: 模型传入的路径字符串。

        返回:
            ``(resolved_path, "")`` 表示成功；``(None, error)`` 表示路径无法解析
            （空串或解析异常）。

        异常:
            不向上抛出。解析失败会被转换成错误字符串。

        副作用:
            无。
        """

        input_error = self._path_input_error(path)
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

        if self._is_blocked_posix_path(path) or self._windows_device_name(path):
            return (
                f"blocked device path: '{path}' refers to an OS device or sensitive "
                "pseudo-file (e.g. NUL/CON/COM1 on Windows, /dev/* or /proc/* on POSIX); "
                "use a regular file path inside the project instead"
            )
        if resolved is not None and (
            self._is_blocked_posix_path(resolved.as_posix())
            or self._windows_device_name(str(resolved))
        ):
            return (
                f"blocked device path: '{path}' resolves to an OS device or sensitive "
                f"pseudo-file ({resolved}); use a regular file path inside the project instead"
            )
        return ""

    def blocked_recursive_search_reason(
        self,
        path: str,
        resolved: Path | None = None,
    ) -> str:
        """判断递归搜索根是否位于设备或敏感伪文件树。

        参数:
            path: 原始搜索根路径。
            resolved: 可选的 unrestricted 解析结果。

        返回:
            空字符串表示允许；非空字符串表示必须拒绝递归搜索。

        异常:
            无。

        副作用:
            无。
        """

        device_reason = self.blocked_device_reason(path, resolved)
        if device_reason:
            return device_reason
        candidates = [str(path)]
        # resolved.as_posix() 是把 Path 对象转换成统一用正斜杠 / 的路径字符串
        if resolved is not None:
            candidates.append(resolved.as_posix())
        for candidate in candidates:
            normalized = candidate.replace("\\", "/").lower().rstrip("/")
            if any(
                normalized == root or normalized.startswith(f"{root}/")
                for root in self.posix_blocked_recursive_roots
            ):
                return f"blocked recursive search path: {path}"
        return ""

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
    def _path_input_error(path: object) -> str:
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
        if "\x00" in path:
            return "path must not contain NUL characters"
        return ""

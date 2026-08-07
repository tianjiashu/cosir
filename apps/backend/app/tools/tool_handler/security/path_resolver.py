"""项目路径安全解析器（PathResolver）。

把 ``read_file`` 原先内联的私有路径安全逻辑提取为独立抽象，供文件工具
共用（符合 Rule of Three，避免路径安全逻辑在多个文件工具间重复）。

设计边界：
- 只解析与拦截路径，不读不写文件。
- 提供三种作用域解析策略，均返回 ``(Path | None, str)`` 二元组：
  - ``resolve_within_workspace``：解析并强制项目根 containment（写/改/删等
    破坏性工具，越界即拒绝）。
  - ``resolve_entry_within_workspace``：解析目录项自身、不跟随末级符号链接
    （删除符号链接自身时用，防借链接逃逸项目根）。
  - ``resolve_without_boundary``：解析但不强制 containment（只读工具，允许
    访问项目根外）。
- ``blocked_device_reason`` 返回空串表示允许。
"""

from pathlib import Path
from typing import ClassVar


class PathResolver:
    """将用户传入的路径解析到项目根目录内的安全解析器。

    单一职责：解析路径并拦截 Windows 设备名与 POSIX 敏感设备/伪文件路径，并提供
    三种作用域策略——:meth:`resolve_within_workspace` 强制项目根 containment
    （供 write / delete / patch 等破坏性工具，越界即拒绝）；
    :meth:`resolve_entry_within_workspace` 解析目录项自身、不跟随末级符号链接
    （供 delete 删除符号链接本身，防越界逃逸）；
    :meth:`resolve_without_boundary` 不强制 containment
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

    def resolve_within_workspace(self, path: str) -> tuple[Path | None, str]:
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

        input_error = self._validate_path(path)
        if input_error:
            return None, input_error
        try:
            #把 workspace 根目录解析成一个「真实、规范化、绝对」的 Path 对象，作为后续边界比较的基准
            # 转成绝对路径：如果原本是相对路径（比如"myproject"），就以当前工作
            # 目录为基准补成绝对路径。
            # 规范化：展开所有的..（上级目录）和.（当前目录），消除多余分隔符。
            # 解析符号链接：在文件系统里跟随符号链接，得到链接指向的真实物理路径。
            root = self.workspace_root.resolve()
            raw = Path(path)
            target = raw if raw.is_absolute() else root / raw

            # 把用户传入的路径 target 解析成一个「真实、规范化、绝对」的路径，存进 resolved
            resolved = target.resolve()

            # 尝试把 resolved 表示为相对于 root 的路径,resolved 是不是 root 的子路径
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"path escapes project root: {path} ({exc})"
        return resolved, ""

    def resolve_entry_within_workspace(self, path: str) -> tuple[Path | None, str]:
        """解析目录项自身，并确认其父目录仍位于项目根内。

        与 :meth:`resolve_within_workspace` 不同，本方法不会跟随最后一级符号链接。
        它只用于删除符号链接本身等必须区分“目录项”和“链接目标”的场景；中间目录
        仍会解析，因而无法借助父级符号链接逃逸项目根。

        参数:
            path: 模型传入的路径字符串。

        返回:
            ``(entry_path, "")`` 表示成功；``(None, error)`` 表示路径非法或越界。

        异常:
            不向上抛出。解析失败会被转换成错误字符串。

        副作用:
            无。
        """

        input_error = self._validate_path(path)
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

    def resolve_without_boundary(self, path: str) -> tuple[Path | None, str]:
        """解析用户路径但不强制项目根 containment（供只读工具使用）。

        与 :meth:`resolve_within_workspace` 的唯一区别是不强制校验路径必须在
        workspace 根内。相对路径仍以 workspace 根为基准解析（保持
        ``read_file("src/x.py")`` 的体验），但解析到 workspace 外的绝对/越界路径
        也允许。项目约定：workspace 内路径只读、可写；workspace 外的路径读是允许
        的、写是禁止的。

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
    def is_inside_workspace(root: Path, path: Path) -> bool:
        """判断路径是否位于 workspace 根内（跟随符号链接的强 containment 判定）。

        用于「未知来源路径」的安全越界检查（如只读 scope 是否越界）：``path`` 可能仍是
        词法路径，需要 ``resolve`` 跟随符号链接后再与根比较，避免借链接逃逸。

        参数:
            root: 当前 workspace 根目录。
            path: 待判断的（可能未解析的）路径。

        返回:
            True 表示 ``path`` 在 ``root`` 内；False 表示越界或无法判定（保守判越界）。

        异常:
            无。

        副作用:
            无。
        """

        try:
            workspace_root = root.resolve(strict=False)
            path.resolve(strict=False).relative_to(workspace_root)
        except (ValueError, OSError, RuntimeError):
            return False
        return True

    @staticmethod
    def escapes_workspace(root: Path, path: Path) -> bool:
        """判断路径是否位于 workspace 根之外。

        参数:
            root: 当前 workspace 根目录。
            path: 已解析的绝对路径。

        返回:
            True 表示 ``path`` 不在 ``root`` 内（允许只读越界但禁止 prepare 遍历）；
            False 表示位于 workspace 内。

        异常:
            无。

        副作用:
            无。
        """

        return not PathResolver.is_inside_workspace(root, path)

    @staticmethod
    def with_workspace_ancestors(root: Path, paths: tuple[Path, ...]) -> tuple[Path, ...]:
        """为写路径补充 workspace 根以下的祖先目录锁。

        参数:
            root: 当前 workspace 根。
            paths: handler 实际涉及的文件或目录路径（已保证在 workspace 内）。

        返回:
            去重后的路径及祖先目录；workspace 根自身不纳入，避免无关顶层目录全串行。

        异常:
            无。

        副作用:
            无。
        """

        workspace_root = root.resolve(strict=False)
        expanded: list[Path] = []
        for path in paths:
            expanded.append(path)
            # 用 relative_to 一次性生成祖先：relative.parents 末项为 "."，天然排除 workspace
            # 根自身；所有祖先均以同一个 workspace_root 实例为基准拼回，无跨表示形式相等比较，
            # 不跟随末级符号链接（path 来自 resolve/resolve_entry，祖先已是真实路径），
            # 从结构上保证锁键不越界、不含根。越界 path 经 is_relative_to 守卫直接跳过。
            if not path.is_relative_to(workspace_root):
                continue
            relative = path.relative_to(workspace_root)
            for ancestor in list(relative.parents)[:-1]:
                expanded.append(workspace_root / ancestor)
        return tuple(dict.fromkeys(expanded))

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
        if "\x00" in path:
            return "path must not contain NUL characters"
        return ""

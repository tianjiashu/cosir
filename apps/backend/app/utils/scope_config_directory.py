"""作用域配置目录的严格读取原语（system 与 workspace 的 ``.cosir`` 子目录共用）。

单一职责：把「某个作用域的配置目录」读成**直接子项 JSON 文件路径列表**，并统一目录级失败
语义。不解析 JSON、不校验业务字段、不缓存、不写日志——失败原因由各配置来源按其事件名记录。

为什么集中：Agent profile 与 Agent Team 两个来源都要回答「这个作用域的配置目录里有哪些
文件」，且都需要「目录不存在 = 没有配置；目录布局被破坏 = 留痕降级」。各写一套会让严格度
漂移（一处拒绝符号链接、一处静默返回空），同一个布局问题在两类配置上一个有日志、一个没有，
事后无法复盘。

不负责：作用域 → 目录的映射（见各来源的 ``directory``）、JSON 解析与字段校验。

目录策略（对两个来源一致）：**只接受真实目录**。目录不存在按「没有配置」处理；失效符号链接、
目录本身是符号链接、路径不是目录（含祖先不是目录，如 ``.cosir`` 被普通文件占位）、配置文件
符号链接越出目录一律视为布局不合法，让调用方降级并留痕。拒绝符号链接目录是为了让「配置从哪
来」在文件系统层面可预测，而不是隐式跟随链接。

已知口径：只识别**小写** ``.json`` 扩展名（沿用 ``Path.glob("*.json")``，因此以点开头的隐藏
JSON 文件不参与装载）。Windows 文件系统大小写不敏感，故 ``X.JSON`` 在 Windows 上会被匹配、
在 macOS/Linux 上不会——写入侧固定写小写扩展名，手工放置时请保持一致。
"""

from __future__ import annotations

from pathlib import Path


class ScopeDirectoryError(ValueError):
    """作用域配置目录布局不合法或不可读。

    取值场景：路径是失效符号链接、目录本身是符号链接、路径不是目录（含祖先不是目录）、
    配置文件符号链接越出目录，或目录不可访问（``OSError`` 统一包装，``FileNotFoundError``
    除外——它表示「没有配置」）。调用方按「该作用域没有配置」降级，并把本异常的消息写进日志。
    """


def read_scope_json_files(directory: str | Path) -> list[Path]:
    """读取作用域配置目录直接子项的 JSON 文件。

    参数:
        directory: 作用域对应的配置目录。

    返回:
        按文件名不分大小写排序的 JSON 文件路径；目录不存在时返回空列表（该作用域没有配置）。

    异常:
        ScopeDirectoryError: 目录布局不合法或目录不可访问（见 :class:`ScopeDirectoryError`）。

    副作用:
        读取配置目录；不创建目录、不修改文件系统、不写日志。
    """

    root = Path(directory)
    # 符号链接先判：``exists()`` 会跟随链接，失效链接的 stat 失败与「目录不存在」无法区分。
    if root.is_symlink():
        if not root.exists():
            raise ScopeDirectoryError(f"配置目录是失效符号链接: {root}")
        raise ScopeDirectoryError(f"配置目录不能是符号链接: {root}")
    try:
        # 显式 stat 而不是 ``exists()``：``exists()`` 会把「祖先不是目录」一起吞成 False，
        # 那会把「配置布局被破坏」误判成「没有配置」而完全不留痕。
        root.stat()
    except FileNotFoundError as exc:
        # 同一个布局问题在 POSIX 上是 ENOTDIR（→ ``NotADirectoryError``），在 Windows 上是
        # ERROR_PATH_NOT_FOUND（→ ``FileNotFoundError``），因此这里还要回看祖先才能区分
        # 「目录尚不存在」与「祖先被普通文件占位」。
        blocking = _nearest_blocking_ancestor(root)
        if blocking is not None:
            raise ScopeDirectoryError(f"配置目录的祖先不是目录: {blocking}") from exc
        return []
    except NotADirectoryError as exc:
        raise ScopeDirectoryError(f"配置目录的父路径不是目录: {root}") from exc
    except OSError as exc:
        raise ScopeDirectoryError(
            f"配置目录不可访问，目录={root}，原因={type(exc).__name__}: {exc}"
        ) from exc

    try:
        if not root.is_dir():
            raise ScopeDirectoryError(f"配置路径不是目录: {root}")
        resolved_root = root.resolve(strict=True)
        files: list[Path] = []
        for path in sorted(root.glob("*.json"), key=lambda item: item.name.casefold()):
            resolved_path = path.resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_root):
                raise ScopeDirectoryError(f"配置文件符号链接越出配置目录: {path}")
            if resolved_path.is_file():
                files.append(path)
        return files
    except ScopeDirectoryError:
        raise
    except (OSError, RuntimeError, UnicodeDecodeError) as exc:
        raise ScopeDirectoryError(
            f"读取配置目录失败，目录={root}，原因={type(exc).__name__}: {exc}"
        ) from exc


def _nearest_blocking_ancestor(path: Path) -> Path | None:
    """返回让 ``path`` 无法作为目录访问的最近祖先。

    用于区分「配置目录尚不存在」（正常的「没有配置」）与「配置目录的某个祖先被普通文件占位」
    （布局被破坏，必须留痕）——后者在 Windows 上会让 ``stat`` 抛 ``FileNotFoundError``，与前者
    的表现完全相同。

    参数:
        path: 待检查的配置目录路径。

    返回:
        存在但不是目录的最近祖先；整条路径只是「还不存在」时返回 ``None``。

    异常:
        无（``Path.exists`` / ``Path.is_dir`` 会吞掉路径解析类错误）。

    副作用:
        无（只读文件系统元数据）。
    """

    candidate = path
    while True:
        if candidate.exists():
            return None if candidate.is_dir() else candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent

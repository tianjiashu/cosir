"""原子写与文件格式检测工具。

本模块只负责把文本安全落盘与检测其格式特征，不解析任何业务语义。

设计边界：
- ``atomic_write_text``：同目录临时文件 + ``os.replace`` 原子替换，避免进程
  崩溃留下半写文件；按目标文件既有 BOM/CRLF 保留行尾与 BOM，避免 Windows
  文件被 LF 化。
- ``detect_line_ending`` / ``detect_bom``：检测文本行尾与 BOM。
- ``looks_like_line_numbered``：识别把 ``read_file`` 输出的 ``N| `` 行号前缀
  整体回写的内容（行号污染），供 write/edit/apply 落盘前拒绝。
"""

import errno
import os
import re
import tempfile
from pathlib import Path

UTF8_BOM = "\ufeff"
UTF8_BOM_BYTES = b"\xef\xbb\xbf"


def atomic_write_text(
    path: Path,
    content: str,
    *,
    preserve_eol: bool = True,
    containment_root: Path | None = None,
) -> None:
    """把文本原子写入目标路径，保留目标文件既有 BOM/CRLF。

    参数:
        path: 已解析到项目根内的目标文件路径。
        content: 待写入的文本内容。
        preserve_eol: 是否按目标文件既有行尾/BOM 保留格式；为 False 时原样写入。
        containment_root: 可选的写边界根目录；提供时会在建目录后和替换前重新校验。

    返回:
        无。

    异常:
        OSError: 当目录创建或文件写入失败时向上抛出。

    副作用:
        在 ``path`` 同目录创建临时文件并通过 ``os.replace`` 原子替换为目标；
        必要时创建父目录。
    """

    target = Path(path)
    target_bom = False
    target_eol: str = "\n"
    if target.exists():
        with target.open("rb") as file:
            sample = file.read(8192).decode("utf-8", errors="replace")
        target_bom = detect_bom(sample) is not None
        target_eol = detect_line_ending(sample)
    else:
        # 新建文件按 content 自身检测 BOM/CRLF 保留（方案 4.2）。
        target_bom = content.startswith(UTF8_BOM)
        target_eol = detect_line_ending(content)

    if preserve_eol:
        text = content.replace("\r\n", "\n")
        text = text.replace("\n", target_eol)
        if target_bom and not text.startswith(UTF8_BOM):
            text = UTF8_BOM + text
        elif not target_bom and text.startswith(UTF8_BOM):
            text = text[1:]
        content_out = text
    else:
        content_out = content

    directory = target.parent
    _assert_existing_ancestor_contained(directory, containment_root)
    directory.mkdir(parents=True, exist_ok=True)
    _assert_contained(target, containment_root)
    file_descriptor, temp_path = tempfile.mkstemp(dir=str(directory), prefix=".tmp_write_")
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as file:
            file.write(content_out)
        _assert_contained(target, containment_root)
        os.replace(temp_path, target)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def _assert_contained(path: Path, containment_root: Path | None) -> None:
    """在实际写入前重新确认目标仍位于 containment 根内。

    参数:
        path: 待写入目标。
        containment_root: 可选 workspace 根；为 None 时跳过校验。

    返回:
        无。

    异常:
        OSError: 当前解析结果越出根目录或无法解析时抛出。

    副作用:
        仅读取路径元数据。
    """

    if containment_root is None:
        return
    try:
        root = Path(containment_root).resolve(strict=True)
        resolved = Path(path).resolve(strict=False)
        resolved.relative_to(root)
        Path(path).parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise OSError(errno.EPERM, f"write path escaped containment root: {path}") from exc


def _assert_existing_ancestor_contained(
    directory: Path,
    containment_root: Path | None,
) -> None:
    """在创建目录前校验最近存在祖先仍位于 containment 根内。

    参数:
        directory: 即将创建或使用的目标父目录。
        containment_root: 可选 workspace 根；为 None 时跳过校验。

    返回:
        无。

    异常:
        OSError: 最近存在祖先越界、为断链 reparse point 或无法解析时抛出。

    副作用:
        仅读取路径元数据，不创建目录。
    """

    if containment_root is None:
        return
    candidate = Path(directory)
    while not os.path.lexists(candidate):
        parent = candidate.parent
        if parent == candidate:
            raise OSError(errno.EPERM, f"no existing ancestor for write path: {directory}")
        candidate = parent
    try:
        root = Path(containment_root).resolve(strict=True)
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise OSError(errno.EPERM, f"write ancestor escaped containment root: {directory}") from exc


def detect_line_ending(text: str) -> str:
    """检测文本使用的行尾风格。

    参数:
        text: 待检测文本。

    返回:
        含回车换行则为 ``"\\r\\n"``，否则为 ``"\\n"``。

    异常:
        无。

    副作用:
        无。
    """

    return "\r\n" if "\r\n" in text else "\n"


def detect_bom(text: str) -> bytes | None:
    """检测文本首部的 UTF-8 BOM。

    参数:
        text: 待检测文本。

    返回:
        检测到 UTF-8 BOM 时返回其字节序列 ``b"\\xef\\xbb\\xbf"``，否则返回 None。

    异常:
        无。

    副作用:
        无。
    """

    if text.startswith(UTF8_BOM):
        return UTF8_BOM_BYTES
    return None


def looks_like_line_numbered(content: str, threshold: float = 0.5) -> bool:
    """判断内容是否像把 ``read_file`` 的带行号输出整体回写（行号污染）。

    参数:
        content: 待检测文本。
        threshold: 命中行号前缀的行占比阈值，超过则判定为行号污染。

    返回:
        True 表示内容疑似行号污染，应当拒绝落盘。

    异常:
        无。

    副作用:
        无。
    """

    if not content:
        return False
    pattern = re.compile(r"^\d+\| ")
    lines = content.split("\n")
    if not lines:
        return False
    counted = sum(1 for line in lines if pattern.match(line))
    return (counted / len(lines)) >= threshold

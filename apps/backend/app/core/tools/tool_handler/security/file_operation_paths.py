"""删除与移动工具共用的「工作区边界内路径」校验。

本模块只做路径与文件形态校验（越界、符号链接、设备路径、UTF-8 文本判定），不执行任何文件
改写；删除与移动两个工具共用同一套路径校验，避免两处各写一份而产生口径漂移。

文本性判定只服务仍要求文本文件的工具（当前为 ``move_file``），并且必须**有界采样**：
``TEXT_SAMPLE_BYTES`` 是唯一允许的采样窗口，禁止为了校验或展示整读文件内容。
``delete_file`` 不判定文本性，也不读取内容。
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

from app.core.tools.tool_handler.security.path_resolver import PathResolver

# 文本性判定的采样窗口：64 KiB 足以覆盖文本文件头部特征，且不随文件体积增长。
TEXT_SAMPLE_BYTES = 64 * 1024

_UTF8_DECODER_FACTORY = codecs.getincrementaldecoder("utf-8")


def resolve_workspace_relative_path(
    resolver: PathResolver,
    value: str,
    *,
    label: str,
) -> tuple[Path | None, str]:
    """解析规范化的工作区相对路径，并拒绝设备路径与越界路径。

    参数:
        resolver: 绑定 workspace 根的路径解析器。
        value: 待解析的路径；允许以 ``\\`` 作分隔符，内部统一归一为 ``/``。
        label: 错误说明中的字段名（如 ``"path"`` / ``"source_path"``），让模型知道是哪个入参
            非法。

    返回:
        ``(解析后的绝对路径, "")``；任一校验失败时返回 ``(None, 错误说明)``。错误说明是面向
        模型的英文短句，会直接进入工具观察的 ``error``。

    异常:
        无：符号链接、设备路径与越界判定都归一化为错误字符串。

    副作用:
        无：不写文件与状态；读取路径元信息并解析符号链接。
    """

    if not isinstance(value, str) or not value.strip():
        return None, f"{label} must be a non-empty workspace-relative path"
    normalized = value.replace("\\", "/")
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        return None, f"{label} must be a normalized workspace-relative path"
    lexical_path = resolver.workspace_root.resolve().joinpath(*normalized.split("/"))
    if lexical_path.is_symlink():
        return None, f"{label} must refer to a regular file, not a symbolic link"
    device_error = resolver.blocked_device_reason(normalized)
    if device_error:
        return None, device_error
    resolved, error = resolver.resolve_within_workspace(normalized)
    if resolved is None:
        return None, error or f"{label} is outside the active workspace"
    device_error = resolver.blocked_device_reason(normalized, resolved)
    if device_error:
        return None, device_error
    return resolved, ""


def ensure_utf8_text_file(
    path: Path,
    *,
    label: str,
    max_bytes: int = TEXT_SAMPLE_BYTES,
) -> str:
    """有界采样校验「普通 UTF-8 文本文件」，返回空串表示通过。

    文件工具不得为了校验或展示读取整个文件：全量读入会让后端进程内存随文件体积增长，并可能
    以 ``MemoryError`` 穿透 handler 归一化。本函数只读目标文件的前 ``max_bytes`` 字节。

    参数:
        path: 待校验的路径，通常来自 :func:`resolve_workspace_relative_path`。
        label: 错误说明中的字段名（如 ``"move source"``），让模型知道是哪个目标非法。
        max_bytes: 采样窗口字节数，必须为正。

    返回:
        ``""`` 表示通过；目标不存在、不是普通文件、不可读、采样窗口内出现非法 UTF-8 字节或
        NUL 字节时，返回面向模型的英文错误说明（``label`` 会嵌入其中）。

    异常:
        ValueError: ``max_bytes`` 不是正数（调用方传参错误，属开发期错误）。
        无其它异常：``OSError`` 与 ``UnicodeDecodeError`` 都归一化为错误字符串。

    副作用:
        只读取目标文件的前 ``max_bytes`` 字节；不写文件、不缓存内容。

    说明:
        采样窗口之外不校验。这是「不整读文件」的代价：窗口外才出现 NUL 或非法 UTF-8 的文件
        会被当作文本文件放行；窗口内的判定与全量校验完全一致。「窗口内」包括两种截然不同的
        截断：读满窗口说明后面还有内容（截断纯属采样造成，不判错），未读满窗口说明已到文件尾
        （文件本身以不完整的多字节序列结尾，按非法 UTF-8 拒绝）。
    """

    if max_bytes < 1:
        raise ValueError("max_bytes must be greater than zero")
    if not path.is_file():
        return f"{label} is not an existing regular file"
    try:
        with path.open("rb") as handle:
            sample = handle.read(max_bytes)
    except OSError as exc:
        return f"{label} is not a readable UTF-8 text file: {exc}"
    # 增量解码器只在 final=True 时报「尾部序列不完整」。两种截断必须区分：
    # - 读满窗口：尾部可能只是被采样切断，用 final=False 让不完整序列留在缓冲区，不判错；
    # - 未读满窗口：尾部就是文件真实结尾，用 final=True 让「EOF 处的截断序列」继续被拒绝，
    #   与全量校验口径一致。
    at_eof = len(sample) < max_bytes
    try:
        text = _UTF8_DECODER_FACTORY().decode(sample, final=at_eof)
    except UnicodeDecodeError as exc:
        return f"{label} is not a readable UTF-8 text file: {exc}"
    if "\x00" in text:
        return f"{label} is binary; only UTF-8 text files are supported"
    return ""

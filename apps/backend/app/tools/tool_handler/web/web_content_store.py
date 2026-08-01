"""网页提取内容的工作区本地存储与模型输出截断。"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings
from app.tools.schemas.tool_execution_context import ToolExecutionContext

_BASE64_IMAGE_PATTERN = re.compile(
    r"!\[([^\]]*)\]\(data:image/[A-Za-z0-9.+-]+;base64,[^)]*\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StoredWebContent:
    """已存入当前工作区的网页提取内容定位信息。

    参数:
        path: 工作区内内容文件的绝对路径。
        relative_path: 供工具调用使用的工作区相对路径。

    返回:
        ``StoredWebContent`` 实例。

    异常:
        无。

    副作用:
        无。
    """

    path: Path
    relative_path: str


def convert_base64_images_to_placeholders(markdown: str) -> str:
    """将 Markdown 内联 base64 图片替换为简洁的文本占位符。

    参数:
        markdown: 可能包含 data URI 图片的 Markdown 文本。

    返回:
        不含内联 base64 图片数据的 Markdown 文本。

    异常:
        无。

    副作用:
        无。
    """

    return _BASE64_IMAGE_PATTERN.sub(_image_placeholder, markdown)


def truncate_or_store_content(
    content: str,
    url: str,
    title: str,
    execution_context: ToolExecutionContext,
    char_limit: int,
) -> tuple[str, StoredWebContent | None]:
    """在超出模型输出预算时保存完整网页内容并生成截断文本。

    参数:
        content: 已清理的完整网页内容。
        url: 内容来源 URL，用于生成稳定文件名。
        title: 页面标题，用于生成可读文件名。
        execution_context: 当前工具执行的工作区边界。
        char_limit: 允许直接传给模型的最大字符数。

    返回:
        未超限时返回原内容和 ``None``；超限时返回首尾截断文本及已存内容定位信息。

    异常:
        ValueError: 当配置的存储目录无法落在当前工作区根目录内时抛出。
        OSError: 当创建目录或写入完整内容失败时抛出。

    副作用:
        内容超限时在当前工作区内创建目录并写入 Markdown 文件。
    """

    if len(content) <= char_limit:
        return content, None

    relative_path = _build_relative_path(url, content, title)
    workspace_root = execution_context.workspace_root.resolve()
    stored_path = workspace_root / relative_path
    _ensure_within_workspace(stored_path, workspace_root)
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_text(content, encoding="utf-8")

    head_budget = char_limit // 2
    tail_budget = char_limit - head_budget
    truncated_content = content[:head_budget] + content[-tail_budget:]
    relative_path_text = relative_path.as_posix()
    footer = (
        "\n\n[Content truncated. Full content saved to "
        f"{relative_path_text}. Use read_file with that path and offset/limit paging "
        "to inspect omitted sections.]"
    )
    return truncated_content + footer, StoredWebContent(
        path=stored_path,
        relative_path=relative_path_text,
    )


def _image_placeholder(match: re.Match[str]) -> str:
    """根据 Markdown 图片匹配生成文本占位符。

    参数:
        match: 内联 base64 图片的正则匹配结果。

    返回:
        含图片替代文本的简洁占位符。

    异常:
        无。

    副作用:
        无。
    """

    return f"[IMAGE: {match.group(1)}]"


def _build_relative_path(url: str, content: str, title: str) -> Path:
    """根据来源与内容构造稳定且安全的工作区相对存储路径。

    参数:
        url: 内容来源 URL。
        content: 完整网页内容。
        title: 页面标题。

    返回:
        位于配置网页提取目录下的 Markdown 相对路径。

    异常:
        无。

    副作用:
        无。
    """

    digest = hashlib.sha256(f"{url}\n{content}".encode()).hexdigest()[:16]
    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "-", title.strip() or "page").strip("-")[:48]
    return Path(Settings.WEB_EXTRACT_STORE_DIR_NAME) / f"{digest}-{safe_title}.md"


def _ensure_within_workspace(path: Path, workspace_root: Path) -> None:
    """验证目标存储路径不会逃逸当前工作区根目录。

    参数:
        path: 待写入的目标路径。
        workspace_root: 当前工具执行允许写入的工作区根目录。

    返回:
        无。

    异常:
        ValueError: 当目标路径不在工作区根目录内时抛出。

    副作用:
        解析文件系统路径，可能访问既有符号链接信息。
    """

    try:
        path.resolve().relative_to(workspace_root)
    except ValueError as error:
        raise ValueError("web content store path must be within the workspace root") from error

"""文件 I/O 纯工具函数与常量。

单一职责：提供不依赖项目类型/模型的纯文件读写工具与文件相关常量。
不负责：路径解析、权限判定、业务逻辑。

模块级常量（如 ``IMAGE_EXTENSIONS``）属于跨层共享的公共契约事实，放在
leaf 层可被 ``api`` / ``core`` / ``service`` 任意层无循环依赖地引用，避免
在 ``conversation_run_state_service`` 与 ``vision_content_blocks`` 之间重复定义同一份扩展名集合。
"""

from pathlib import Path

from app.models.attachment_ref import AttachmentRef


def render_attachment_refs_to_text(attachments: list[AttachmentRef]) -> str:
    """把非图片附件引用渲染为模型可读的文本前缀块。

    图片附件不进入文本（走多模态 block 通道），故本函数仅处理 ``file`` /
    ``directory`` / ``url`` 三类。渲染结果形如：

        [附件参考]
        - 文件: /a/b.py
        - 目录: /src/ （请用 list_directory / search_files 探索）
        - 链接: https://example.com （请用 web_extract 抓取）

    参数:
        attachments: 已分类的附件列表（图片应事先过滤掉）。

    返回:
        多行文本块；当 ``attachments`` 为空时返回空字符串（调用方据此决定是否拼接）。

    异常:
        无。

    副作用:
        无。
    """
    if not attachments:
        return ""
    lines = ["[附件参考]"]
    for att in attachments:
        if att.kind == "file":
            lines.append(f"- 文件: {att.ref}")
        elif att.kind == "directory":
            lines.append(f"- 目录: {att.ref} （请用 list_directory / search_files 探索）")
        elif att.kind == "url":
            lines.append(f"- 链接: {att.ref} （请用 web_extract 抓取）")
    return "\n".join(lines)


def read_text_file(path: str | Path) -> str:
    """读取文本文件的全部内容。

    参数:
        path: 文件路径（字符串或 Path 对象）。

    返回:
        文件的 UTF-8 文本内容。

    异常:
        FileNotFoundError: 文件不存在。
        PermissionError: 无权读取文件。
        OSError: 其他 I/O 错误（如磁盘满、路径非法）。

    副作用:
        无。
    """
    return Path(path).read_text(encoding="utf-8")

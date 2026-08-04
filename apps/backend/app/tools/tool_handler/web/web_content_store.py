"""网页提取内容的模型输出清洗。

本模块只承载内联 base64 图片占位替换这一项清洗职责。超长正文的截断与
落盘统一上收至 ``app.tools.guard.tool_output_budget.ToolOutputBudget``，避免与
文件 IO / 路径安全基础设施重复实现（该模块已使用 ``ProjectPathResolver`` 与
``atomic_write_text`` 完成 workspace 边界约束与原子写）。
"""

import re

_BASE64_IMAGE_PATTERN = re.compile(
    r"!\[([^\]]*)\]\(data:image/[A-Za-z0-9.+-]+;base64,[^)]*\)",
    re.IGNORECASE,
)


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

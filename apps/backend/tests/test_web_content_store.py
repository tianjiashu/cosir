from app.tools.tool_handler.web.web_content_store import (
    convert_base64_images_to_placeholders,
)


def test_replaces_inline_base64_images_with_placeholder() -> None:
    """验证内联 base64 图片被替换为简洁占位符。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        无。
    """

    content = "Before ![diagram](data:image/png;base64,AAAABBBB) after"

    assert convert_base64_images_to_placeholders(content) == "Before [IMAGE: diagram] after"

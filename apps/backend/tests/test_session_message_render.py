"""用户输入的起始标签、图片清单与展示绑定遵守渲染契约。"""

import re

from app.core.workflow.message_queue.session_message import RunLaunch, UserInputBody
from app.models.conversation_input import UserInputPresentation


def _user_input(text: str, *image_paths: str) -> UserInputBody:
    return UserInputBody(
        text=text,
        launch=RunLaunch(model_config_id="test/m1"),
        presentation=UserInputPresentation(text=text, image_paths=list(image_paths)),
    )


def _tagged_text(body: UserInputBody) -> str:
    """渲染时包装出来的文本块（有图时在 content 列表的第一段）。"""

    content = body.render()[0]["content"]
    if isinstance(content, str):
        return content
    text_part = content[0]
    assert text_part["type"] == "text"
    return text_part["text"]


def test_user_input_preserves_the_original_text() -> None:
    original = "  第一行\n比较 a > b\n"
    _, _, payload = _tagged_text(_user_input(original)).partition(">")
    assert payload.removesuffix("</user_input>") == original


def test_user_input_lists_each_image_path_on_its_own_line() -> None:
    _, _, payload = _tagged_text(_user_input("看图", "/a.png", "/b.png")).partition(">")
    assert payload.removesuffix("</user_input>").splitlines()[1:] == [
        "image_paths:",
        "- /a.png",
        "- /b.png",
    ]


def test_user_input_without_images_has_no_image_paths_field() -> None:
    assert "image_paths:" not in _tagged_text(_user_input("hi"))


def test_output_layer_hint_is_in_the_opening_tag() -> None:
    opening, _, payload = _tagged_text(_user_input("hi")).partition(">")
    assert opening.startswith("<user_input ")
    assert re.search(r' hint="[^"]*\boutput_layer\b[^"]*"', opening)
    assert "output_layer" not in payload


def test_user_input_with_images_only_still_carries_its_tag() -> None:
    message, _ = _user_input("", "/a.png").render()
    text_part, image_part = message["content"]
    assert text_part["type"] == "text"
    assert text_part["text"].startswith("<user_input ")
    assert text_part["text"].endswith("</user_input>")
    assert image_part["type"] == "image_url"

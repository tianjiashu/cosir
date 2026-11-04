"""用户输入的正文、图片清单与起始标签提示各自遵守渲染契约。"""

import re

from app.core.workflow.message_queue.task_message import RunLaunch, UserInputBody
from app.models.conversation_input import UserInputPresentation


def _user_input(text: str, *image_paths: str) -> UserInputBody:
    return UserInputBody(
        input_text=text,
        launch=RunLaunch(model_config_id=1),
        presentation=UserInputPresentation(display_text=text, image_paths=list(image_paths)),
    )


def test_user_input_preserves_the_original_text() -> None:
    original = "  第一行\n比较 a > b\n"
    _, _, payload = _user_input(original).text.partition(">")
    assert payload.removesuffix("</user_input>") == original


def test_user_input_lists_each_image_path_on_its_own_line() -> None:
    body = _user_input("看图", "/a.png", "/b.png")
    _, _, payload = body.text.partition(">")
    assert payload.removesuffix("</user_input>").splitlines()[1:] == [
        "image_paths:", "- /a.png", "- /b.png",
    ]


def test_user_input_without_images_has_no_image_paths_field() -> None:
    assert "image_paths:" not in _user_input("hi").text


def test_output_layer_hint_is_in_the_opening_tag() -> None:
    opening, _, payload = _user_input("hi").text.partition(">")
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

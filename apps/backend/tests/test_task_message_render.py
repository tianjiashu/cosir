"""Task 消息渲染：标签里只放结构化字段，读法说明归系统提示词。"""

from app.core.workflow.message_queue.task_message import RunLaunch, UserInputBody
from app.models.conversation_input import UserInputPresentation


def test_user_input_lists_each_image_path_on_its_own_line() -> None:
    body = UserInputBody(
        input_text="看图",
        launch=RunLaunch(model_config_id=1),
        presentation=UserInputPresentation(display_text="看图", image_paths=["/a.png", "/b.png"]),
    )

    assert body.text.endswith(">看图\nimage_paths:\n- /a.png\n- /b.png</user_input>")


def test_user_input_without_images_has_no_image_paths_field() -> None:
    body = UserInputBody(
        input_text="hi",
        launch=RunLaunch(model_config_id=1),
        presentation=UserInputPresentation(display_text="hi"),
    )

    assert "image_paths" not in body.text

"""Task 消息渲染：标签里只放结构化字段，读法说明归系统提示词。"""

from app.core.workflow.inbox.task_message import RunLaunch, UserInputBody


def test_user_input_lists_each_image_path_on_its_own_line() -> None:
    body = UserInputBody(
        display_text="看图",
        input_text="看图",
        image_paths=["/a.png", "/b.png"],
        launch=RunLaunch(model_config_id=1),
    )

    assert body.text.endswith(">看图\nimage_paths:\n- /a.png\n- /b.png</user_input>")


def test_user_input_without_images_has_no_image_paths_field() -> None:
    body = UserInputBody(display_text="hi", input_text="hi", launch=RunLaunch(model_config_id=1))

    assert "image_paths" not in body.text

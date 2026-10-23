"""代码示例和引用片段经普通消息链路传递时不激活附件标记。"""

import re
from types import SimpleNamespace

import pytest

from app.assistant_transport.projection.user_input_projection import build_user_input_parts
from app.models.conversation_run_command import ConversationRunCommand
from app.service.conversation_run import user_input_service as user_input_module
from app.service.conversation_run.user_input_service import UserInputService
from app.utils.message_tokens import find_message_tokens, replace_message_tokens


@pytest.fixture
def prepare(monkeypatch):
    """经生产入口 ``UserInputService.prepare`` 解析纯文本命令，返回模型正文与展示绑定。"""
    monkeypatch.setattr(
        user_input_module,
        "get_model_config_service",
        lambda: SimpleNamespace(
            get_config=lambda _id: SimpleNamespace(
                supports_reasoning_effort=False, supports_image=False
            )
        ),
    )
    service = object.__new__(UserInputService)
    service._task = SimpleNamespace(get=lambda _task_id: SimpleNamespace(workspace_id=7))
    return lambda text: service.prepare(1, ConversationRunCommand(display_text=text), 1).message


@pytest.mark.parametrize(
    "literal",
    [
        "`[[cosir-file:missing]]`",
        "``a ` [[cosir-file:missing]]``",
        "```py\n[[cosir-file:missing]]\n[[cosir-image:missing]]\n```",
        "~~~\n[[cosir-file:missing]]\n~~~",
        "```\r\n[[cosir-file:missing]]\r\n```",
        "    [[cosir-file:missing]]",
        ">     [[cosir-file:missing]]",
        "```\n[[cosir-file:missing]]",
        "> ```\n> [[cosir-file:missing]]\n> ```",
    ],
)
def test_code_reaches_model_and_history_without_attachment_registry(literal, prepare):
    prepared = prepare(literal)
    assert prepared.input_text == literal
    assert prepared.display_text == literal
    assert prepared.attachments == []
    assert build_user_input_parts(literal, [], []) == [
        {"type": "text", "text": literal, "status": "completed"}
    ]


def test_tokens_outside_code_remain_active_and_offsets_preserve_original(prepare):
    pattern = re.compile(r"\[\[x\]\]")
    text = "[[x]] `[[x]]`\r\n\r\n```\r\n[[x]]\r\n```\r\n[[x]]"
    assert len(list(find_message_tokens(pattern, text))) == 2
    assert (
        replace_message_tokens(pattern, "yes", text) == text.replace("[[x]]", "yes", 1)[:-5] + "yes"
    )
    with pytest.raises(ValueError, match="unavailable"):
        prepare("[[cosir-file:missing]]")


def test_workspace_markdown_is_ordinary_text(prepare):
    text = (
        "[a](cosir-file:deleted.py)\n\n[a](cosir-snippet:a.py#L1-L2)\n\n```\n"
        "<!-- [[cosir-file:missing]] -->\n[[cosir-skill:test]]\n```"
    )
    prepared = prepare(text)
    assert prepared.input_text == text
    assert build_user_input_parts(text, [], [])[0]["text"] == text

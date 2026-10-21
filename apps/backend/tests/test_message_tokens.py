"""代码示例和引用片段经普通消息链路传递时不激活附件标记。"""

import re

import pytest

from app.assistant_transport.projection.user_input_projection import build_user_input_parts
from app.models.conversation_run_command import ConversationRunCommand
from app.service.conversation_run.conversation_input_preparation_service import (
    ConversationInputPreparationService,
)
from app.utils.message_tokens import find_message_tokens, replace_message_tokens


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
def test_code_reaches_model_and_history_without_attachment_registry(literal):
    service = object.__new__(ConversationInputPreparationService)
    prepared = service._prepare_command(
        None,
        ConversationRunCommand(display_text=literal),
        supports_image=False,
    )
    assert prepared.input_text == literal
    assert prepared.display_text == literal
    assert prepared.attachments == []
    assert build_user_input_parts(literal, [], []) == [
        {"type": "text", "text": literal, "status": "completed"}
    ]


def test_tokens_outside_code_remain_active_and_offsets_preserve_original():
    pattern = re.compile(r"\[\[x\]\]")
    text = "[[x]] `[[x]]`\r\n\r\n```\r\n[[x]]\r\n```\r\n[[x]]"
    assert len(list(find_message_tokens(pattern, text))) == 2
    assert (
        replace_message_tokens(pattern, "yes", text) == text.replace("[[x]]", "yes", 1)[:-5] + "yes"
    )
    with pytest.raises(ValueError, match="unavailable"):
        ConversationInputPreparationService.__new__(
            ConversationInputPreparationService
        )._prepare_command(
            None,
            ConversationRunCommand(display_text="[[cosir-file:missing]]"),
            supports_image=False,
        )


def test_workspace_markdown_is_ordinary_text():
    text = (
        "[a](cosir-file:deleted.py)\n\n[a](cosir-snippet:a.py#L1-L2)\n\n```\n"
        "<!-- [[cosir-file:missing]] -->\n[[cosir-skill:test]]\n```"
    )
    service = object.__new__(ConversationInputPreparationService)
    prepared = service._prepare_command(
        None,
        ConversationRunCommand(display_text=text),
        supports_image=False,
    )
    assert prepared.input_text == text
    assert build_user_input_parts(text, [], [])[0]["text"] == text

"""胶囊标签只在 Markdown 代码之外生效；代码区域判定只按给定正则找匹配，不解释标签含义。"""

import re

import pytest

from app.core.capsules.code_regions import find_outside_code

_PATTERN = re.compile(r"\[\[x\]\]")


@pytest.mark.parametrize(
    "literal",
    [
        "`[[x]]`",
        "``a ` [[x]]``",
        "```py\n[[x]]\n[[x]]\n```",
        "~~~\n[[x]]\n~~~",
        "```\r\n[[x]]\r\n```",
        "    [[x]]",
        ">     [[x]]",
        "```\n[[x]]",
        "> ```\n> [[x]]\n> ```",
    ],
)
def test_marker_inside_code_is_not_found(literal: str) -> None:
    assert list(find_outside_code(_PATTERN, literal)) == []


def test_marker_outside_code_is_found_with_original_offsets() -> None:
    text = "[[x]] `[[x]]`\r\n\r\n```\r\n[[x]]\r\n```\r\n[[x]]"

    matches = list(find_outside_code(_PATTERN, text))

    assert [match.group(0) for match in matches] == ["[[x]]", "[[x]]"]
    assert [match.start() for match in matches] == [0, text.rindex("[[x]]")]


def test_text_without_markers_yields_nothing() -> None:
    assert list(find_outside_code(_PATTERN, "普通文本")) == []

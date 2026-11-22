"""用户输入胶囊语法的跨语言用例：与前端共用一份 fixture，防止两侧语法漂移。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.models.message_context.capsules.capsule_grammar import (
    CapsuleSegment,
    Segment,
    TextSegment,
)
from app.models.message_context.capsules.grammar.capsule import Capsule
from app.models.message_context.capsules.grammar.file_capsule import FileCapsule
from app.models.message_context.capsules.grammar.ref_capsule import RefCapsule

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "desktop"
    / "lib"
    / "assistant"
    / "capsules"
    / "contract"
    / "capsule-grammar-cases.json"
)
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _capsule_doc(capsule: Capsule) -> dict[str, object]:
    if isinstance(capsule, FileCapsule):
        return {"kind": "file", "path": capsule.path}
    if isinstance(capsule, RefCapsule):
        return {
            "kind": "ref",
            "path": capsule.path,
            **({"lines": capsule.lines} if capsule.lines is not None else {}),
            **({"page": capsule.page} if capsule.page is not None else {}),
        }
    return {
        "kind": "skill",
        "name": capsule.name,
        **({"path": capsule.path} if capsule.path is not None else {}),
    }


def _segment_doc(segment: Segment) -> dict[str, object]:
    if isinstance(segment, CapsuleSegment):
        return {"type": "capsule", "capsule": _capsule_doc(segment.capsule)}
    if isinstance(segment, TextSegment):
        return {"type": "text", "text": segment.text}
    raise TypeError(f"unsupported segment: {type(segment).__name__}")


@pytest.mark.parametrize("case", _CASES["cases"], ids=[case["name"] for case in _CASES["cases"]])
def test_capsule_case(case: dict[str, object]) -> None:
    text = str(case["text"])
    segments = Segment.parse(text)
    assert [_segment_doc(segment) for segment in segments] == case["segments"]
    assert Segment.serialize_all(segments) == case["canonical"]


@pytest.mark.parametrize("text", _CASES["notCapsules"])
def test_invalid_capsule_stays_text(text: str) -> None:
    assert Segment.parse(text) == [TextSegment(text=text)]

"""``ask_user``：参数与回答的校验、回执格式、工具声明（设计见 ``docs/plan/ask-user-design.md``）。

通用 HITL 链路（载荷入队、并入时结算占位、回合作废、挂起唤醒）见 ``test_tool_decision_entry.py``、
``test_hitl_decision.py``、``test_dispatch_policy.py`` 与 ``test_transport_command_chain.py``。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.agents.define_agents import general_child_agent, main_agent
from app.core.tool.handler.ask_user import AskUserTool, build_ask_user_definition
from app.core.tool.handler.question.ask_user_answers import AskUserAnswers
from app.core.tool.handler.question.ask_user_args import AskUserArgs
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema


def _option(label: str) -> dict[str, Any]:
    return {"label": label, "description": f"choose {label}"}


def _question(text: str = "Which store?", *, multi: bool = False, **overrides: Any) -> dict[str, Any]:
    return {
        "question": text,
        "header": "Store",
        "options": [_option("Cookie"), _option("Local")],
        "multi_select": multi,
        **overrides,
    }


_ARGS = {"questions": [_question(), _question("Which events?", multi=True)]}


def _answers(**data: Any) -> AskUserAnswers:
    return AskUserAnswers.model_validate(data, context={"args": _ARGS})


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "questions",
    [
        [],
        [_question(f"Q{i}?") for i in range(5)],
        [_question(options=[_option("Only")])],
        [_question(options=[_option(str(i)) for i in range(5)])],
        [_question(header="thirteen chars")],
        [_question(), _question()],
        [_question(options=[_option("Same"), _option("Same")])],
    ],
    ids=["none", "five", "one-option", "five-options", "long-header", "dup-question", "dup-label"],
)
def test_arguments_out_of_contract_are_rejected(questions: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        AskUserArgs.model_validate({"questions": questions})


def test_preview_is_accepted_and_schema_is_strict() -> None:
    option = {**_option("Cookie"), "preview": "<code/>"}
    AskUserArgs.model_validate({"questions": [_question(options=[option, _option("Local")])]})

    schema = strict_parameters_schema(AskUserArgs)
    assert schema["additionalProperties"] is False


# ---------------------------------------------------------------------------
# 回答
# ---------------------------------------------------------------------------


def test_reply_follows_the_question_order_and_lists_skipped_questions() -> None:
    answers = _answers(answers={"Which events?": ["Cookie", "on logout"]})

    assert answers.reply() == (
        'The user answered: "Which store?"="(no answer)", "Which events?"="Cookie, on logout". '
        "Read the answers carefully — they may request clarification, changes, or that you not "
        "proceed — and follow what they actually say."
    )


def test_other_text_and_notes_are_carried_verbatim() -> None:
    answers = _answers(
        answers={"Which store?": "a signed header"},
        annotations={"Which store?": {"notes": "no cookies on this host"}},
    )

    assert '"Which store?"="a signed header" (user notes: no cookies on this host)' in answers.reply()
    assert answers.model_dump(mode="json") == {
        "answers": {"Which store?": "a signed header"},
        "annotations": {"Which store?": {"notes": "no cookies on this host"}},
    }


@pytest.mark.parametrize(
    "data",
    [
        {"answers": {}},
        {"answers": {"Not asked?": "Cookie"}},
        {"answers": {"Which store?": ["Cookie", "Local"]}},
        {"answers": {"Which store?": "  "}},
        {"answers": {"Which events?": "Cookie"}},
        {"answers": {"Which events?": []}},
        {"answers": {"Which events?": ["Cookie", "Cookie"]}},
        {"answers": {"Which events?": ["one custom", "two custom"]}},
        {"answers": {"Which store?": "Cookie"}, "annotations": {"Which events?": {"notes": "x"}}},
    ],
    ids=[
        "nothing-answered", "unknown-question", "single-with-list", "blank", "multi-with-string",
        "multi-empty", "multi-duplicate", "two-custom", "note-on-unanswered",
    ],
)
def test_answers_that_do_not_fit_the_call_are_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _answers(**data)


# ---------------------------------------------------------------------------
# 工具声明
# ---------------------------------------------------------------------------


def test_first_call_registers_the_question_and_suspends() -> None:
    questions = AskUserArgs.model_validate(_ARGS).model_dump(mode="json")["questions"]

    observation = AskUserTool().execute(
        questions=questions, execution_context=SimpleNamespace(tool_call_id="call-ask")
    )

    assert observation.suspend is True and observation.awaiting_decision is True
    assert observation.display_data == {"kind": "user-question", "questions": questions}


def test_definition_declares_a_turn_scoped_reply_decision() -> None:
    definition = build_ask_user_definition()

    assert (definition.user_decision, definition.decision_lifetime) == ("reply", "turn")
    assert definition.decision_model is AskUserAnswers
    # 需要用户作答的工具只给能操作界面的 Session：子 Agent 的工具清单里没有它。
    assert definition.requires_user_surface is True


def test_only_the_main_agent_is_given_the_tool() -> None:
    assert "ask_user" in main_agent().allowed_tools
    assert "ask_user" not in general_child_agent().allowed_tools


# ---------------------------------------------------------------------------
# 链路：模型写下的原始调用 → 门禁 → 提问 → 用户作答
# ---------------------------------------------------------------------------


def test_answer_is_checked_against_the_call_as_the_model_wrote_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """原始调用参数带着 ``intent``：门禁与决定入口取到同一份业务参数，回答照常被接受。

    决定入口曾直接拿原始参数核对回答，``intent`` 被提问的参数模型当成非法字段，所有回答都被拒。
    """
    import json

    from app.core.tool.validation.call_arguments import resolve_call_arguments
    from app.service.conversation import tool_decision_service

    definition = build_ask_user_definition().normalized()
    raw = json.dumps({"intent": "确认登录方案", **_ARGS}, ensure_ascii=False)
    monkeypatch.setattr(
        tool_decision_service,
        "get_tool_registry",
        lambda: SimpleNamespace(get_tool_definition=lambda _name: definition),
    )

    asked = resolve_call_arguments(definition, raw)
    observation = definition.handler(
        **asked.arguments, execution_context=SimpleNamespace(tool_call_id="call-ask")
    )
    payload, reply = tool_decision_service._validated_payload(
        "ask_user", raw, "approve", {"answers": {"Which store?": "Cookie"}}
    )

    assert asked.intent == "确认登录方案"
    assert observation.display_data["questions"] == asked.arguments["questions"]
    assert payload == {"answers": {"Which store?": "Cookie"}, "annotations": {}}
    assert reply is not None and '"Which store?"="Cookie"' in reply

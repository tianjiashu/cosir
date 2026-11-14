"""用户对一次 ``ask_user`` 提问的回答：决定载荷（见 ``DecisionPayload``）。"""

from typing import Any, ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationInfo, model_validator

from app.core.tool.handler.question.ask_user_args import AskUserArgs
from app.core.tool.schemas.decision_payload import DecisionPayload


class AnswerAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str = Field(min_length=1)


class AskUserAnswers(DecisionPayload):
    """照 Claude Code：``answers`` 以问题原文为键，值是所选 ``label``（多选为列表）或「Other」里
    填的文字；跳过的题不出现。``annotations`` 同样以问题原文为键。

    全部跳过不走这里：那是驳回、不带载荷（``DECLINED_REPLY``），所以至少要答一题。校验对照原调用
    参数（``info.context["args"]``）：键必须是这次提问里的问题，单选题只收一个值，多选题的每一项要么
    是该题的 ``label``、要么是至多一条自由文字。单选题的值不要求属于选项——「Other」填的就是任意文字。
    """

    DECLINED_REPLY: ClassVar[str] = (
        "The user declined to answer. Adjust your approach instead of asking the same question again."
    )

    model_config = ConfigDict(extra="forbid")

    answers: dict[str, str | list[str]] = Field(min_length=1)
    annotations: dict[str, AnswerAnnotation] = Field(default_factory=dict)
    # 原调用里的问题顺序：回执按提问顺序写，并把没答的题列出来。校验时从上下文取，不入载荷。
    _asked: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _answers_match_the_questions(self, info: ValidationInfo) -> "AskUserAnswers":
        asked = AskUserArgs.model_validate(cast(dict[str, Any], info.context)["args"])
        questions = {question.question: question for question in asked.questions}
        self._asked = list(questions)
        unknown = (set(self.answers) | set(self.annotations)) - set(questions)
        if unknown:
            raise ValueError(f"not asked in this call: {sorted(unknown)}")
        if set(self.annotations) - set(self.answers):
            raise ValueError("annotations are only allowed on answered questions")
        for text, answer in self.answers.items():
            question = questions[text]
            if not question.multi_select:
                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError(f"a single-select question takes one answer: {text!r}")
                continue
            if not isinstance(answer, list) or not answer or len(set(answer)) != len(answer):
                raise ValueError(f"a multi-select question takes a list of distinct answers: {text!r}")
            labels = {option.label for option in question.options}
            custom = [item for item in answer if item not in labels]
            if len(custom) > 1 or any(not item.strip() for item in answer):
                raise ValueError(f"at most one custom answer per question: {text!r}")
        return self

    def reply(self) -> str:
        """照 Claude Code 的回执，按提问顺序：多选的各项用逗号连接，批注附在对应回答后，跳过的题
        写 ``(no answer)``。"""
        parts = []
        for question in self._asked:
            answer = self.answers.get(question)
            if answer is None:
                parts.append(f'"{question}"="(no answer)"')
                continue
            text = answer if isinstance(answer, str) else ", ".join(answer)
            part = f'"{question}"="{text}"'
            note = self.annotations.get(question)
            if note is not None:
                part += f" (user notes: {note.notes})"
            parts.append(part)
        return (
            f"The user answered: {', '.join(parts)}. Read the answers carefully — they may request "
            "clarification, changes, or that you not proceed — and follow what they actually say."
        )

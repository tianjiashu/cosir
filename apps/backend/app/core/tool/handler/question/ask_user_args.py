"""``ask_user`` 工具的参数模型；字段说明照录 Claude Code ``AskUserQuestion``（参数名改蛇形）。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuestionOption(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    label: str = Field(
        min_length=1,
        description=(
            "The display text for this option that the user will see and select. Should be "
            "concise (1-5 words) and clearly describe the choice."
        ),
    )
    description: str = Field(
        min_length=1,
        description=(
            "Explanation of what this option means or what will happen if chosen. Useful for "
            "providing context about trade-offs or implications."
        ),
    )
    preview: str | None = Field(
        default=None,
        description=(
            "Optional preview content rendered when this option is focused. Use for mockups, "
            "code snippets, or visual comparisons that help users compare options."
        ),
    )


class Question(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    question: str = Field(
        min_length=1,
        description=(
            "The complete question to ask the user. Should be clear, specific, and end with a "
            'question mark. Example: "Which library should we use for date formatting?" If '
            'multi_select is true, phrase it accordingly, e.g. "Which features do you want to '
            'enable?"'
        ),
    )
    header: str = Field(
        min_length=1,
        max_length=12,
        description=(
            "Very short label displayed as a chip/tag (max 12 chars). Examples: "
            '"Auth method", "Library", "Approach".'
        ),
    )
    options: list[QuestionOption] = Field(
        min_length=2,
        max_length=4,
        description=(
            "The available choices for this question. Must have 2-4 options. Each option should "
            "be a distinct, mutually exclusive choice (unless multi_select is enabled). There "
            "should be no 'Other' option, that will be provided automatically."
        ),
    )
    multi_select: bool = Field(
        default=False,
        description=(
            "(default false) Set to true to allow the user to select multiple options instead "
            "of just one. Use when choices are not mutually exclusive."
        ),
    )

    @model_validator(mode="after")
    def _labels_are_unique(self) -> "Question":
        labels = [option.label for option in self.options]
        if len(set(labels)) != len(labels):
            raise ValueError("option labels must be unique within a question")
        return self


class AskUserArgs(BaseModel):
    """一次提问：1–4 题。回答以问题原文作键，所以同一次调用里问题不能重复。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    questions: list[Question] = Field(
        min_length=1, max_length=4, description="Questions to ask the user (1-4 questions)"
    )

    @model_validator(mode="after")
    def _questions_are_unique(self) -> "AskUserArgs":
        texts = [question.question for question in self.questions]
        if len(set(texts)) != len(texts):
            raise ValueError("questions must be unique within one call")
        return self

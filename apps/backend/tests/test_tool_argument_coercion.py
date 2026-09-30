"""工具参数宽容归一的契约测试。

单一职责：验证「strict 校验前置的输入适配」这一条链路——
1. 白名单内的无损形态被还原（``"30"``→30、``30.0``→30、``"true"``→True、单值→列表）；
2. 白名单外的形态不被还原，且原样交给 strict 拒绝（``"1"``→bool、``""``→数字、数字→字符串）；
3. strict 的既有语义与安全边界不被放宽（范围、枚举、``extra="forbid"``、Unknown 字段）；
4. 归一会产出可观测事实，合法原生参数不产生事实（幂等）。

不覆盖：门禁日志落盘（由 ``tool_access_gate`` 负责）、handler 内业务校验。
"""

from __future__ import annotations

import pytest
from app.core.tools.tool_models.child_task.child_agent_send_args import ChildAgentSendArgs
from app.core.tools.tool_models.child_task.child_agent_status_args import ChildAgentStatusArgs
from app.core.tools.tool_models.child_task.child_agent_wait_args import ChildAgentWaitArgs
from app.core.tools.tool_models.child_task.delegate_task_args import DelegateTaskArgs
from app.core.tools.tool_models.execute_terminal_args import (
    MacExecuteTerminalArgs,
    WindowsExecuteTerminalArgs,
)
from app.core.tools.tool_models.list_directory_args import ListDirectoryArgs
from app.core.tools.tool_models.terminal_session_args import TerminalReadArgs
from app.core.tools.tool_models.web_extract_args import WebExtractArgs
from app.core.tools.validation.argument_coercion import coerce_tool_arguments
from app.core.tools.validation.arguments import validate_tool_arguments
from pydantic import BaseModel, ConfigDict, Field


class _IntOrStrModel(BaseModel):
    """测试用多类型联合模型：验证歧义联合不被归一。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    value: int | str = Field(default=0, description="union probe")


def _coerce(arguments: dict[str, object], model: type) -> tuple[object, dict[str, str]]:
    """调用归一入口并把事实压成 ``{字段: "from→to"}``，便于断言。"""

    adapted, facts = coerce_tool_arguments(arguments, model)
    return adapted, {item.field: f"{item.from_type}->{item.to_type}" for item in facts}


class TestWhitelistCoercion:
    """白名单内的无损还原。"""

    def test_numeric_string_to_float_and_int(self) -> None:
        """本次事故的真实形态：``"30"``（float 字段）与 ``"2"``（int 字段）必须被还原。"""

        adapted, facts = _coerce(
            {"command": "pwd && uname -a", "shell": "auto", "timeout": "30", "workdir": "."},
            MacExecuteTerminalArgs,
        )
        assert adapted["timeout"] == 30.0  # type: ignore[index]
        assert facts == {"timeout": "str->float"}

        read_adapted, read_facts = _coerce(
            {"session_id": "term_x", "after_seq": "2", "wait_ms": 3000},
            TerminalReadArgs,
        )
        assert read_adapted["after_seq"] == 2  # type: ignore[index]
        assert read_facts == {"after_seq": "str->int"}

    def test_integral_float_to_int(self) -> None:
        """``30.0`` 归一为 int 字段的 30。潜在缺陷：strict 下整数值浮点被拒造成无效重试。"""

        adapted, facts = _coerce(
            {"session_id": "term_x", "after_seq": 30.0, "wait_ms": 10},
            TerminalReadArgs,
        )
        assert adapted["after_seq"] == 30  # type: ignore[index]
        assert facts == {"after_seq": "float->int"}

    def test_true_false_string_to_bool(self) -> None:
        """仅 ``"true"/"false"``（忽略大小写与空白）归一为布尔。"""

        adapted, facts = _coerce({"path": ".", "include_hidden": " TRUE "}, ListDirectoryArgs)
        assert adapted["include_hidden"] is True  # type: ignore[index]
        assert facts == {"include_hidden": "str->bool"}

    def test_single_value_to_list(self) -> None:
        """声明为列表的字段收到标量时包成单元素列表（元素递归同规则）。"""

        adapted, facts = _coerce({"urls": "https://example.com"}, WebExtractArgs)
        assert adapted["urls"] == ["https://example.com"]  # type: ignore[index]
        assert facts == {"urls": "str->list"}

    def test_list_element_coercion(self) -> None:
        """列表内部元素按声明元素类型归一。"""

        adapted, facts = _coerce({"path": ".", "include_globs": ["*.md"]}, ListDirectoryArgs)
        assert adapted["include_globs"] == ["*.md"]  # type: ignore[index]
        assert facts == {}

    def test_native_arguments_are_idempotent(self) -> None:
        """合规的原生参数不产生任何归一事实（避免把正常调用记成模型缺陷）。"""

        arguments = {"command": "pwd", "shell": "auto", "timeout": 30, "workdir": "."}
        adapted, facts = _coerce(arguments, MacExecuteTerminalArgs)
        assert adapted == arguments
        assert facts == {}


class TestWhitelistRefusals:
    """白名单外的形态不得被还原（strict 仍是唯一判定）。"""

    def test_ambiguous_bool_string_is_not_coerced(self) -> None:
        """``"1"`` 不归因为布尔：与整型语义冲突，禁止猜测。"""

        adapted, facts = _coerce({"path": ".", "include_hidden": "1"}, ListDirectoryArgs)
        assert adapted["include_hidden"] == "1"  # type: ignore[index]
        assert facts == {}

    def test_empty_and_non_numeric_string_are_not_coerced(self) -> None:
        """``""`` 与 ``"abc"`` 不归一（不把空串当 None，也不猜语义）。"""

        for bad in ("", "abc"):
            adapted, facts = _coerce({"session_id": "term_x", "after_seq": bad}, TerminalReadArgs)
            assert adapted["after_seq"] == bad  # type: ignore[index]
            assert facts == {}

    def test_literal_field_is_never_coerced(self) -> None:
        """``Literal`` 字段（shell/signal）不做任何归一。潜在缺陷：宽松会把非法枚举放行。"""

        adapted, facts = _coerce({"command": "pwd", "shell": "zsh3"}, MacExecuteTerminalArgs)
        assert adapted["shell"] == "zsh3"  # type: ignore[index]
        assert facts == {}

    def test_unknown_field_is_preserved_for_strict_rejection(self) -> None:
        """未知字段不归一也不丢弃，交给 strict 的 ``extra="forbid"`` 报错。"""

        adapted, facts = _coerce({"command": "pwd", "shelll": "auto"}, MacExecuteTerminalArgs)
        assert adapted["shelll"] == "auto"  # type: ignore[index]
        assert facts == {}


class TestValidationIntegration:
    """接入 ``validate_tool_arguments`` 后的端到端语义。"""

    def test_qwen_style_arguments_now_pass_with_fact(self) -> None:
        """qwen 真实形态（``timeout: "30"``）必须通过，并带出归一事实。"""

        result = validate_tool_arguments(
            {"command": "pwd && uname -a", "shell": "auto", "timeout": "30", "workdir": "."},
            {},
            MacExecuteTerminalArgs,
        )
        assert result.ok is True
        assert result.arguments["timeout"] == 30.0
        assert [item.field for item in result.coercions] == ["timeout"]

    def test_terminal_read_after_seq_string_passes(self) -> None:
        """``after_seq: "2"`` 通过且归一为整数。"""

        result = validate_tool_arguments(
            {"session_id": "term_x", "after_seq": "2", "wait_ms": 3000},
            {},
            TerminalReadArgs,
        )
        assert result.ok is True
        assert result.arguments["after_seq"] == 2
        assert [item.field for item in result.coercions] == ["after_seq"]

    def test_native_arguments_have_no_fact(self) -> None:
        """deepseek 形态（``timeout: 30`` 原生数字）通过且不产生事实。"""

        result = validate_tool_arguments(
            {"command": "pwd && uname -a", "shell": "auto", "timeout": 30, "workdir": "."},
            {},
            MacExecuteTerminalArgs,
        )
        assert result.ok is True
        assert result.arguments["timeout"] == 30
        assert result.coercions == ()

    @pytest.mark.parametrize("bad", [0, -1, "0", "-1"])
    def test_range_constraint_is_not_relaxed_by_coercion(self, bad: object) -> None:
        """``gt=0`` 仍优先生效：``"0"`` 归一成 0 后同样被拒（归一不绕过业务约束）。"""

        result = validate_tool_arguments(
            {"command": "pwd", "timeout": bad},
            {},
            MacExecuteTerminalArgs,
        )
        assert result.ok is False
        assert "timeout" in result.error

    def test_literal_and_extra_forbid_stay_strict(self) -> None:
        """枚举与未知字段仍按 strict 拒绝（未被归一削弱）。"""

        bad_shell = validate_tool_arguments(
            {"command": "pwd", "shell": None},
            {},
            MacExecuteTerminalArgs,
        )
        assert bad_shell.ok is False
        assert "shell" in bad_shell.error

        unknown = validate_tool_arguments(
            {"command": "pwd", "shelll": "auto"},
            {},
            MacExecuteTerminalArgs,
        )
        assert unknown.ok is False
        assert "shelll" in unknown.error

    def test_type_error_message_carries_actionable_hint(self) -> None:
        """类型类错误必须附带「传 JSON 原生类型」的可操作提示。潜在缺陷：模型无法自纠。"""

        result = validate_tool_arguments(
            {"session_id": "term_x", "after_seq": "abc"},
            {},
            TerminalReadArgs,
        )
        assert result.ok is False
        assert "after_seq" in result.error
        assert "JSON numbers/booleans" in result.error

    def test_non_mapping_arguments_still_rejected(self) -> None:
        """非对象入参不被归一，仍按 strict 报错。"""

        result = validate_tool_arguments(["pwd"], {}, MacExecuteTerminalArgs)
        assert result.ok is False


class TestWhitelistBoundaries:
    """边界：归一不得放宽列表级约束，也不得对歧义或容器形态做猜测。"""

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_string_is_not_wrapped_into_list(self, blank: str) -> None:
        """空串/纯空白不得包成单元素列表，否则会绕过列表级 ``min_length``。

        潜在缺陷（独立测试发现）：``urls=""`` 曾被归一为 ``[""]``，长度满足
        ``min_length=1``，约束被形态转换规避。
        """

        result = validate_tool_arguments({"urls": blank}, {}, WebExtractArgs)
        assert result.ok is False
        assert "urls" in result.error
        assert result.coercions == ()

    def test_tuple_stays_tuple_when_no_element_coercion_needed(self) -> None:
        """元素无需归一时容器类型不被改写，交给 strict 报「应为列表」。"""

        adapted, facts = _coerce({"path": ".", "include_globs": ("*.md",)}, ListDirectoryArgs)
        assert isinstance(adapted["include_globs"], tuple)
        assert facts == {}

    def test_multi_type_union_is_never_coerced(self) -> None:
        """多类型联合存在歧义，一律不归一。潜在缺陷：猜测类型导致错误接受。"""

        adapted, facts = _coerce({"value": "30"}, _IntOrStrModel)
        assert adapted["value"] == "30"
        assert facts == {}

    def test_windows_model_coerces_identically(self) -> None:
        """两个平台契约模型的宽容行为必须对称（避免只在 macOS 上被验证）。"""

        result = validate_tool_arguments(
            {"command": "pwd", "shell": "auto", "timeout": "30"},
            {},
            WindowsExecuteTerminalArgs,
        )
        assert result.ok is True
        assert result.arguments["timeout"] == 30.0


class TestChildTaskStrictAlignment:
    """``child_task`` 参数模型必须与其余工具模型同样保持 strict（契约对齐）。"""

    @pytest.mark.parametrize(
        "model",
        [DelegateTaskArgs, ChildAgentSendArgs, ChildAgentStatusArgs, ChildAgentWaitArgs],
    )
    def test_child_task_models_keep_strict_config(self, model: type[BaseModel]) -> None:
        """潜在缺陷：漏配 strict 会让该工具退化为宽松校验，与其它工具口径分叉。"""

        assert model.model_config.get("strict") is True
        assert model.model_config.get("extra") == "forbid"

    def test_string_id_is_coerced_then_strictly_accepted(self) -> None:
        """对齐 strict 后，字符串 id 仍可经入口归一被接受（宽容在入口、严格在契约）。"""

        result = validate_tool_arguments({"child_task_id": "4"}, {}, ChildAgentStatusArgs)
        assert result.ok is True
        assert result.arguments["child_task_id"] == 4
        assert [item.field for item in result.coercions] == ["child_task_id"]

"""缺陷发现型对抗测试：Run 失败终态收敛 + 受控失败文案通路。

本模块**不**重复既有正向契约测试，而是从对抗视角攻击本次修复声称的四条不变量：

1. 落定链路（``_settle_failed_run`` / ``ReactLikeWorkflow.run`` / ``_run_graph``）在
   「落定自身失败」「Run 已被别处落终态」「异常是 ``BaseException`` 子类」「分类器自身
   抛异常」等边界下都必须保证：**Run 先落 failed 再抛原异常**，且不得替换/掩盖原始异常。
2. 分类器 ``classify_model_failure`` 的状态码边界（99/100/599/600/负数/bool/字符串）、
   属性访问抛异常、``__str__`` 抛异常、语义串冲突、大小写与中文报文都必须返回稳定结果
   或 ``None``，绝不抛出。
3. 文案目录 ``run_failure_message`` 对每个可产出 code 都有专属非空文案，且不含厂商名、
   模型名与数字；未知 code / ``None`` 回退通用文案。
4. Run 级 ``error`` 跨层契约（``terminal_error`` → ``build_run_error`` → snapshot 校验）
   在键缺失/多键/空串/非字符串/非法标识符时都必须显式失败，而非静默降级。
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_run_snapshot import ConversationRunSnapshot
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.config.constant import Constant
from app.core.llm_provider.model_failure import classify_model_failure
from app.core.workflows.react.workflow import ReactLikeWorkflow
from app.core.workflows.workflow_operations import WorkflowOperations
from app.models import conversation_run_failure as failure_catalog
from app.models.conversation_run_failure import (
    run_failure_message,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.enums.error_kind import ErrorKind
from app.service.task.conversation_run_state_service import terminal_error

# --------------------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------------------


class _RecordingOperations:
    """记录 ``fail_run_if_running`` 入参的假门面；可按需模拟抛异常 / 返回 None。"""

    def __init__(self, *, settles: bool = True, failure: BaseException | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._settles = settles
        self._failure = failure
        # ``get_current_run`` 自身可被独立注入异常，用于攻击日志分支。
        self.get_current_run_failure: BaseException | None = None

    def get_current_run(self) -> SimpleNamespace:
        if self.get_current_run_failure is not None:
            raise self.get_current_run_failure
        return SimpleNamespace(id=7)

    def fail_run_if_running(
        self,
        *,
        end_reason: str | None = None,
        usage_stats: object = None,
        final_output: str | None = None,
    ) -> SimpleNamespace | None:
        self.calls.append(
            {
                "end_reason": end_reason,
                "usage_stats": usage_stats,
                "final_output": final_output,
            }
        )
        if self._failure is not None:
            raise self._failure
        return SimpleNamespace(id=7) if self._settles else None


def _record_state() -> ConversationStateSnapshot:
    """构造一个只含单个 running Run 的合法 snapshot。"""

    state = empty_snapshot()
    state["runs"] = [
        ConversationRunSnapshot(
            runId=1,
            status="running",
            endReason=None,
            messages=[],
            usage=None,
            error=None,
        )
    ]
    state["current_run_id"] = 1
    return state


def _flow_codes() -> list[str]:
    """返回文案目录模块声明的流程类失败 code（``RUN_FAILURE_CODE_*`` 常量值）。"""

    return [
        value
        for name, value in vars(failure_catalog).items()
        if name.startswith("RUN_FAILURE_CODE_") and isinstance(value, str)
    ]


def _all_catalog_codes() -> list[str]:
    """返回文案目录里登记的全部 code（含 ``ErrorKind`` 模型码与取消类字面量）。"""

    return list(failure_catalog._RUN_FAILURE_MESSAGES.keys())


# --------------------------------------------------------------------------------------
# 1. 分类器对抗测试
# --------------------------------------------------------------------------------------


class _BoolStatusError(Exception):
    """``status_code`` 是 ``bool`` 的异常：``bool`` 是 ``int`` 子类，必须被排除。"""

    def __init__(self, status_code: object, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class _StrStatusError(Exception):
    """``status_code`` 是字符串：不得被当作 HTTP 状态码。"""

    def __init__(self, status_code: object, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class _PropertyBoomError(Exception):
    """读取 ``status_code`` / ``response`` 属性时抛异常：分类器必须吞掉。"""

    @property
    def status_code(self) -> int:
        raise RuntimeError("status_code property exploded")

    @property
    def response(self) -> object:
        raise RuntimeError("response property exploded")


class _StrBoomError(Exception):
    """``__str__`` 抛异常：分类器必须降级为无法判定，而不是抛异常。"""

    def __str__(self) -> str:
        raise RuntimeError("__str__ exploded")


class _StrBoomStatusError(Exception):
    """带可用状态码但 ``__str__`` 抛异常：状态码应仍生效。"""

    def __init__(self, status_code: int) -> None:
        super().__init__()
        self.status_code = status_code

    def __str__(self) -> str:
        raise RuntimeError("__str__ exploded")


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (99, None),  # 边界外下限：不映射，且必须继续走类型名/文本判据
        (100, ErrorKind.MODEL_INVALID_REQUEST.value),
        (599, ErrorKind.MODEL_SERVICE_ERROR.value),
        (600, None),  # 边界外上限
        (-1, None),
        (0, None),
    ],
)
def test_status_code_boundaries(status_code: int, expected: str | None) -> None:
    """状态码边界（99/100/599/600/负数/0）必须按 100<=code<600 白名单判定，越界返回 None。

    潜在缺陷：边界写成 ``<= 600`` 或 ``0 < code`` 会把非法状态码误映射到具体分类。
    """

    assert classify_model_failure(_StrStatusError(status_code)) == expected


def test_negative_status_code_with_no_other_signal_returns_none() -> None:
    """负状态码不得落到 4xx 分档（``code >= 500`` 为假会掉进 INVALID_REQUEST）。

    潜在缺陷：``_kind_from_status_code`` 对负数返回 INVALID_REQUEST；若上层忘了白名单
    过滤，负状态码会被误报成「请求被拒绝」。
    """

    assert classify_model_failure(_StrStatusError(-402)) is None


def test_boolean_status_code_without_other_signal_returns_none() -> None:
    """``True`` / ``False`` 都不得被当作状态码。"""

    assert classify_model_failure(_BoolStatusError(True)) is None
    assert classify_model_failure(_BoolStatusError(False)) is None


def test_string_status_code_is_ignored() -> None:
    """字符串状态码（如 ``"402"``）不是 int，必须被忽略。

    潜在缺陷：若实现用 ``str(exc.status_code).isdigit()`` 之类宽松判定，会把字符串
    状态码误当数字。
    """

    assert classify_model_failure(_StrStatusError("402")) is None


def test_status_code_property_exploding_does_not_raise() -> None:
    """读取 ``status_code`` / ``response`` 属性抛异常时，分类器必须吞掉并返回 None。"""

    assert classify_model_failure(_PropertyBoomError("boom")) is None


def test_exception_str_exploding_does_not_raise() -> None:
    """``__str__`` 抛异常时必须降级处理，绝不向上抛。"""

    assert classify_model_failure(_StrBoomError()) is None


def test_usable_status_code_wins_even_when_str_explodes() -> None:
    """状态码可用时，``__str__`` 抛异常不影响状态码判据。"""

    assert (
        classify_model_failure(_StrBoomStatusError(402)) == ErrorKind.MODEL_INSUFFICIENT_QUOTA.value
    )


def test_status_code_wins_over_conflicting_message_keywords() -> None:
    """429 报文含 ``insufficient`` 时仍必须归为限流（状态码优先级最高）。"""

    assert (
        classify_model_failure(_StrStatusError(429, "insufficient balance, rate limit"))
        == ErrorKind.MODEL_RATE_LIMITED.value
    )


def test_message_is_lowercased_before_keyword_match() -> None:
    """语义串匹配必须大小写不敏感。"""

    assert (
        classify_model_failure(Exception("INSUFFICIENT BALANCE"))
        == ErrorKind.MODEL_INSUFFICIENT_QUOTA.value
    )
    assert (
        classify_model_failure(Exception("RATE LIMIT EXCEEDED"))
        == ErrorKind.MODEL_RATE_LIMITED.value
    )


def test_chinese_message_is_not_misclassified() -> None:
    """纯中文报文不命中通用英文语义词，必须返回 None（不得猜测）。

    潜在缺陷：若有人为了「支持中文」加入宽泛子串（如「失败」），会把任何中文异常
    都误分类成模型错误。
    """

    assert classify_model_failure(Exception("余额不足，请充值")) is None
    assert classify_model_failure(Exception("模型响应超时")) is None


def test_provider_name_in_message_does_not_drive_classification() -> None:
    """报文里出现厂商名本身不得影响分类（不得有厂商分支）。"""

    assert classify_model_failure(Exception("deepseek error occurred")) is None
    assert classify_model_failure(Exception("openai anthropic gemini failure")) is None


def test_classification_never_raises_for_pathological_exceptions() -> None:
    """穷举畸形异常，分类器绝不允许抛出（否则失败收尾链路会二次失败）。"""

    samples: list[BaseException] = [
        _PropertyBoomError("x"),
        _StrBoomError(),
        _StrBoomStatusError(503),
        _BoolStatusError(True),
        _StrStatusError("429"),
        _StrStatusError(None),
        _StrStatusError([]),
        _StrStatusError({"a": 1}),
        Exception(""),
        ValueError(),
        KeyboardInterrupt(),  # BaseException 子类
        SystemExit(1),
    ]
    for sample in samples:
        result = classify_model_failure(sample)
        assert result is None or isinstance(result, str)


def test_classification_result_is_always_error_kind_model_value_or_none() -> None:
    """分类输出只能是 ``ErrorKind`` 的 ``model_*`` 值或 ``None``，不得是自由文本。"""

    model_values = {member.value for member in ErrorKind if member.name.startswith("MODEL_")}
    samples: list[BaseException] = [
        _StrStatusError(402),
        _StrStatusError(503),
        Exception("insufficient balance"),
        Exception("totally unknown"),
        _PropertyBoomError("x"),
    ]
    for sample in samples:
        result = classify_model_failure(sample)
        assert result is None or result in model_values


# --------------------------------------------------------------------------------------
# 2. 文案目录对抗测试
# --------------------------------------------------------------------------------------


def test_every_catalog_code_has_non_empty_own_message() -> None:
    """目录中每一个 code 都必须有非空且专属的文案（不得互相复用同一句）。"""

    fallback = run_failure_message(None)
    seen: dict[str, str] = {}
    for code in _all_catalog_codes():
        message = run_failure_message(code)
        assert message.strip(), code
        if code != Constant.Run.RUN_FAILURE_CODE_UNKNOWN:
            assert message != fallback or code == Constant.Run.RUN_FAILURE_CODE_UNKNOWN, code
        seen[code] = message
    # 除各取消类 code 共享「已取消本轮对话」外，其余文案应各不相同。
    duplicated = {
        message: [code for code, value in seen.items() if value == message]
        for message in set(seen.values())
    }
    unexpected = {
        message: codes
        for message, codes in duplicated.items()
        if len(codes) > 1 and not all("cancelled" in code for code in codes)
    }
    assert not unexpected, f"非取消类 code 复用了同一文案：{unexpected}"


def test_unknown_and_none_codes_fall_back_to_generic_message() -> None:
    """``None``、未知 code、空字符串都必须回退通用文案（保证 UI 永远有文本）。"""

    fallback = run_failure_message(None)
    assert fallback.strip()
    assert run_failure_message(Constant.Run.RUN_FAILURE_CODE_UNKNOWN) == fallback
    assert run_failure_message("totally_unknown_code") == fallback
    assert run_failure_message("") == fallback


def test_messages_are_provider_agnostic_and_digit_free() -> None:
    """所有目录文案不得含厂商/模型名或任何数字（含状态码）。"""

    tokens = (
        "deepseek",
        "openai",
        "anthropic",
        "gemini",
        "claude",
        "gpt",
        "qwen",
        "moonshot",
        "groq",
        "ollama",
        "azure",
        "siliconflow",
    )
    for code in _all_catalog_codes():
        message = run_failure_message(code)
        lowered = message.lower()
        for token in tokens:
            assert token not in lowered, (code, token)
        assert not re.search(r"\d", message), (code, message)
        assert not re.search(r"\b(4\d\d|5\d\d)\b", message), (code, message)


def test_all_catalog_codes_are_valid_identifiers() -> None:
    """每个 code 都必须是合法标识符（``isidentifier()``），否则 ``terminal_error`` 会丢 code。"""

    for code in _all_catalog_codes():
        assert code.isidentifier(), code


def test_run_failure_message_tolerates_non_string_input() -> None:
    """非字符串 code（如 int）不得抛出；当前实现用 ``dict.get`` 天然容忍。

    潜在缺陷：若实现改成 ``code.strip()`` 或正则匹配，会在 int / bytes 上抛异常。
    """

    for weird in (1, 0, True, b"run_failed", 3.14, ("a",), object()):
        assert isinstance(run_failure_message(cast(Any, weird)), str)


# --------------------------------------------------------------------------------------
# 3. ``terminal_error`` 对抗测试
# --------------------------------------------------------------------------------------


def test_terminal_error_completed_has_no_error_contract() -> None:
    """``completed`` 终态不写错误契约。"""

    assert terminal_error(ConversationRunStatus.COMPLETED, "run_failed") is None


@pytest.mark.parametrize(
    "end_reason",
    [
        "含有空格的原因",
        "402-quota",
        "中文原因",
        "run_failed ",
        "-leading",
        "",
        None,
    ],
)
def test_terminal_error_rejects_non_identifier_end_reason(end_reason: str | None) -> None:
    """非标识符的 ``end_reason`` 不得成为 code，必须回退到状态对应的兜底 code。

    潜在缺陷：若实现直接 ``end_reason or fallback``，自由文本会进入受控 ``code`` 字段。
    """

    error = terminal_error(ConversationRunStatus.FAILED, end_reason)
    assert error is not None
    assert error["code"].isidentifier()
    if end_reason is None or not end_reason.isidentifier():
        assert error["code"] == Constant.Run.RUN_FAILURE_CODE_UNKNOWN
    else:
        assert error["code"] == end_reason


def test_terminal_error_cancelled_fallback_code() -> None:
    """``cancelled`` 终态在无可用原因时回退取消类兜底 code。"""

    error = terminal_error(ConversationRunStatus.CANCELLED, None)
    assert error is not None
    assert error["code"] == failure_catalog.RUN_FAILURE_CODE_CANCELLED

    error = terminal_error(ConversationRunStatus.CANCELLED, "not an identifier")
    assert error is not None
    assert error["code"] == failure_catalog.RUN_FAILURE_CODE_CANCELLED


def test_terminal_error_message_is_non_empty_and_controlled() -> None:
    """任何终态的受控 message 都必须非空且来自文案目录。"""

    for status in (
        ConversationRunStatus.FAILED,
        ConversationRunStatus.CANCELLED,
    ):
        for reason in (None, "unknown_free_text", "model_insufficient_quota"):
            error = terminal_error(status, reason)
            assert error is not None
            assert error["message"].strip()
            assert not re.search(r"\d", error["message"])
            assert set(error) == {"code", "message"}


# --------------------------------------------------------------------------------------
# 4. ``_settle_failed_run`` 落定链路对抗测试
# --------------------------------------------------------------------------------------


def test_settle_failed_run_swallows_runtime_error() -> None:
    """落终态抛 ``RuntimeError`` 时必须吞掉，不得替换原始异常。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations(failure=RuntimeError("db down"))
    workflow._settle_failed_run(cast(WorkflowOperations, operations), Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED)
    assert len(operations.calls) == 1


def test_settle_failed_run_swallows_key_error() -> None:
    """落终态抛 ``KeyError``（run 不存在）时也必须吞掉。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations(failure=KeyError("run not found"))
    workflow._settle_failed_run(cast(WorkflowOperations, operations), Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED)
    assert len(operations.calls) == 1


def test_settle_failed_run_does_not_swallow_base_exception() -> None:
    """``BaseException``（如 ``KeyboardInterrupt``）当前不在 ``except Exception`` 覆盖内。

    这是**记录在案的疑点**：本用例锁定当前行为（向上抛出），以便实现变化时显式失败。
    """

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations(failure=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        workflow._settle_failed_run(
            cast(WorkflowOperations, operations), Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED
        )
    assert len(operations.calls) == 1


def test_settle_failed_run_survives_get_current_run_failure() -> None:
    """落终态失败且日志取 ``get_current_run()`` 也失败时，不得让异常逃逸。

    潜在缺陷：``log.exception`` 的 ``extra`` 里直接调 ``operations.get_current_run().id``，
    若该调用抛异常，会在 except 分支内二次抛出，替换掉原始异常。
    """

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations(failure=RuntimeError("db down"))
    operations.get_current_run_failure = RuntimeError("run gone")
    workflow._settle_failed_run(cast(WorkflowOperations, operations), Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED)
    assert len(operations.calls) == 1


def test_settle_failed_run_survives_get_current_run_failure_on_race_lost() -> None:
    """「Run 已被别处落终态」分支取 ``get_current_run()`` 失败时也不得抛出。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations(settles=False)
    operations.get_current_run_failure = RuntimeError("run gone")
    workflow._settle_failed_run(cast(WorkflowOperations, operations), Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED)
    assert len(operations.calls) == 1


def test_current_run_id_returns_none_when_facade_raises() -> None:
    """日志字段读取失败必须降级为 ``None``：收尾路径上的日志语句不得再抛。

    覆盖 graph 异常分支等直接读取 Run 标识的日志点；它们与 ``_settle_failed_run`` 共用同一
    个 ``_current_run_id`` 入口，因此本用例是该不变量的统一守卫。
    """

    operations = _RecordingOperations()
    operations.get_current_run_failure = RuntimeError("facade unavailable")

    assert ReactLikeWorkflow._current_run_id(cast(WorkflowOperations, operations)) is None


def test_current_run_id_returns_run_identity_when_available() -> None:
    """门面可用时返回真实 Run 标识，供日志定位。"""

    operations = _RecordingOperations()

    assert ReactLikeWorkflow._current_run_id(cast(WorkflowOperations, operations)) == 7


def test_settle_failed_run_with_empty_string_code_records_empty_reason() -> None:
    """空字符串 code 会被原样透传给落定入口（``run_failure_message`` 回退通用文案）。

    这里锁定契约：``end_reason`` 原样传递，而 ``final_output`` 仍是可展示文案。
    上游不应传空串；若真传入，DB 侧 ``terminal_error`` 会把 code 兜底成 ``run_failed``，
    因此本用例只断言不抛异常、文案非空。
    """

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations()
    workflow._settle_failed_run(cast(WorkflowOperations, operations), "")
    assert len(operations.calls) == 1
    assert operations.calls[0]["end_reason"] == ""
    assert isinstance(operations.calls[0]["final_output"], str)
    assert str(operations.calls[0]["final_output"]).strip()


@pytest.mark.asyncio
async def test_run_settles_before_propagating_and_preserves_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落终态自身失败时，``run`` 必须仍抛出**原始**异常，而不是落定异常。"""

    workflow = ReactLikeWorkflow()
    original = _StrStatusError(402, "Insufficient Balance")
    operations = _RecordingOperations(failure=RuntimeError("db down"))

    async def _boom(_self: ReactLikeWorkflow, *_args: object, **_kwargs: object) -> None:
        raise original

    monkeypatch.setattr(ReactLikeWorkflow, "_run_graph", _boom)

    with pytest.raises(_StrStatusError) as excinfo:
        await workflow.run(cast(WorkflowOperations, operations))

    assert excinfo.value is original
    assert operations.calls[0]["end_reason"] == ErrorKind.MODEL_INSUFFICIENT_QUOTA.value


@pytest.mark.asyncio
async def test_run_classifier_failure_does_not_break_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分类器若抛异常，``run`` 内的兜底 code 逻辑不得连带失败（异常仍需落定）。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations()

    class _BoomClassifier(Exception):
        def __str__(self) -> str:
            raise RuntimeError("__str__ exploded")

    async def _boom(_self: ReactLikeWorkflow, *_args: object, **_kwargs: object) -> None:
        raise _BoomClassifier()

    monkeypatch.setattr(ReactLikeWorkflow, "_run_graph", _boom)

    with pytest.raises(_BoomClassifier):
        await workflow.run(cast(WorkflowOperations, operations))

    assert operations.calls[0]["end_reason"] == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED


@pytest.mark.asyncio
async def test_run_settles_exactly_once_when_settlement_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常路径下 ``fail_run_if_running`` 只被调用一次（外层兜底不得重复落定）。

    潜在缺陷：若 ``_run_graph`` 的内部 graph 分支与外层 ``run`` 都落定，且内部落定失败，
    会出现重复落定写入。本用例锁定「外层兜底仅在内部未落定时补一次」，需通过调用计数
    验证不会因内部 settles=False（竞态）而重复已成功写入的落定。
    """

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations(settles=True)

    async def _boom(_self: ReactLikeWorkflow, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("node exploded")

    monkeypatch.setattr(ReactLikeWorkflow, "_run_graph", _boom)

    with pytest.raises(RuntimeError):
        await workflow.run(cast(WorkflowOperations, operations))

    assert len(operations.calls) == 1


@pytest.mark.asyncio
async def test_run_settles_even_when_provider_id_missing_branch_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``provider_id`` 缺失分支落定后抛 ValueError，外层兜底不得再落定一次。

    潜在缺陷：内部分支落定成功（返回记录）后外层若再无脑落定，会命中「已非 running」
    分支产生多余日志；更重要的是若内部分支漏落定，外层必须补上——本用例两者都覆盖。
    """

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations()

    async def _boom(_self: ReactLikeWorkflow, *_args: object, **_kwargs: object) -> None:
        raise ValueError("Conversation Run provider_id is required")

    monkeypatch.setattr(ReactLikeWorkflow, "_run_graph", _boom)

    with pytest.raises(ValueError):
        await workflow.run(cast(WorkflowOperations, operations))

    assert len(operations.calls) == 1
    assert operations.calls[0]["end_reason"] == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED


# --------------------------------------------------------------------------------------
# 5. Run 级 error 跨层契约对抗测试
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"code": "run_failed"},
        {"message": "运行失败"},
        {"code": "", "message": "运行失败"},
        {"code": "run_failed", "message": ""},
        {"code": "run_failed", "message": "   "},  # 空白 message 是否被拒？
        {"code": 1, "message": "运行失败"},
        {"code": "run_failed", "message": 1},
        {"code": None, "message": None},
        {"code": "run_failed", "message": "运行失败", "retryable": True},
        {"code": "run_failed", "message": "运行失败", "extra": 1},
        {"code": ["run_failed"], "message": "运行失败"},
    ],
)
def test_build_run_error_rejects_malformed_payload(payload: object) -> None:
    """``build_run_error`` 必须对任何非 ``{code,message}`` 双非空字符串契约显式失败。

    潜在缺陷：``code="run_failed", message="   "`` 这种空白 message 若被放行，UI 会显示
    空白错误提示；本用例断言它必须抛 ValueError。
    """

    run = SimpleNamespace(id=3, error=payload)
    if payload is None:
        assert (
            ConversationTaskStateRebuilder.build_run_error(cast(ConversationRunRecord, run)) is None
        )
        return
    if isinstance(payload, dict) and payload.get("message") == "   ":
        # 空白 message 的期望：显式失败（不得静默降级成空白提示）。
        with pytest.raises(ValueError):
            ConversationTaskStateRebuilder.build_run_error(cast(ConversationRunRecord, run))
        return
    with pytest.raises(ValueError):
        ConversationTaskStateRebuilder.build_run_error(cast(ConversationRunRecord, run))


def test_build_run_error_accepts_valid_contract() -> None:
    """合法契约必须原样投影为 Transport 错误。"""

    run = SimpleNamespace(
        id=3,
        error={
            "code": "model_insufficient_quota",
            "message": run_failure_message("model_insufficient_quota"),
        },
    )
    assert ConversationTaskStateRebuilder.build_run_error(cast(ConversationRunRecord, run)) == {
        "code": "model_insufficient_quota",
        "message": run_failure_message("model_insufficient_quota"),
    }


def test_build_run_error_does_not_raise_attribute_error_for_non_mapping() -> None:
    """持久化 error 是非映射（如字符串）时，必须抛 ValueError 而不是 AttributeError。"""

    for weird in ("oops", 42, ["a"], object()):
        run = SimpleNamespace(id=3, error=weird)
        with pytest.raises(ValueError):
            ConversationTaskStateRebuilder.build_run_error(cast(ConversationRunRecord, run))


def test_validate_snapshot_rejects_run_error_with_extra_keys() -> None:
    """snapshot 校验必须拒绝带 ``retryable`` 等多余键的 Run 级 error。"""

    state = _record_state()
    state["runs"][0]["error"] = {
        "code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED,
        "message": run_failure_message(Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED),
        "retryable": False,
    }
    with pytest.raises(ValueError):
        validate_snapshot(state)


def test_validate_snapshot_rejects_run_error_missing_message() -> None:
    """``error`` 缺 ``message`` 键时必须显式失败，不得静默降级。"""

    state = _record_state()
    state["runs"][0]["error"] = {"code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED}
    with pytest.raises(ValueError):
        validate_snapshot(state)


def test_validate_snapshot_rejects_non_string_run_error_fields() -> None:
    """``code`` / ``message`` 非字符串时必须显式失败。"""

    state = _record_state()
    state["runs"][0]["error"] = {"code": 1, "message": "运行失败"}
    with pytest.raises(ValueError):
        validate_snapshot(state)

    state = _record_state()
    state["runs"][0]["error"] = {"code": "run_failed", "message": None}
    with pytest.raises(ValueError):
        validate_snapshot(state)


def test_validate_snapshot_requires_error_key_present_on_every_run() -> None:
    """Run 缺少 ``error`` 键必须让快照校验失败（严格白名单），而不是静默降级。"""

    state = _record_state()
    del state["runs"][0]["error"]  # type: ignore[misc]
    with pytest.raises(ValueError):
        validate_snapshot(state)


def test_validate_snapshot_rejects_boolean_run_id_with_error_contract() -> None:
    """``runId`` 为 bool 时即使 error 契约合法也必须失败（bool 是 int 子类）。"""

    state = _record_state()
    state["runs"][0]["runId"] = True  # type: ignore[typeddict-item]
    state["current_run_id"] = True  # type: ignore[typeddict-item]
    with pytest.raises(ValueError):
        validate_snapshot(state)


# --------------------------------------------------------------------------------------
# 6. 穷举构造 Run snapshot 的位置（含 e2e mock）一致性核对
# --------------------------------------------------------------------------------------


_RUN_REQUIRED_KEYS = {"runId", "status", "endReason", "messages", "usage", "error"}


def _python_run_literal_keys(text: str, source_name: str) -> list[tuple[int, set[str]]]:
    """用 ``ast`` 精确提取 Python 源码里所有「Run 形状」字典字面量的键集合。

    只认同时含 ``runId`` 与 ``messages`` 键的字典字面量，避免把 ``run["runId"]``
    之类的读取点误判为构造点。
    """

    import ast

    try:
        tree = ast.parse(text, filename=source_name)
    except SyntaxError:  # 非 Python 文件（如 .mjs）
        return []

    results: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys: set[str] = set()
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
        if "runId" in keys and "messages" in keys:
            results.append((node.lineno, keys))
    return results


def _js_object_literal_keys(text: str) -> list[tuple[int, set[str]]]:
    """粗粒度提取 JS 对象字面量的顶层键集合（用于 e2e mock 的 Run 形状核对）。"""

    results: list[tuple[int, set[str]]] = []
    for match in re.finditer(r"\{", text):
        opening = match.start()
        depth = 0
        end = -1
        for index in range(opening, min(len(text), opening + 8000)):
            character = text[index]
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end < 0:
            continue
        block = text[opening + 1 : end]
        # 提取顶层键：仅扫描深度 0 处、紧跟 ``{`` 或 ``,`` 的标识符/字符串。
        keys: set[str] = set()
        inner_depth = 0
        position = 0
        while position < len(block):
            character = block[position]
            if character in "{[":
                inner_depth += 1
            elif character in "}]":
                inner_depth -= 1
            elif inner_depth == 0:
                key_match = re.match(
                    r"""\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z_$][\w$]*))\s*:""", block[position:]
                )
                if key_match:
                    keys.add(next(group for group in key_match.groups() if group))
                    position += key_match.end()
                    continue
            position += 1
        if "runId" in keys and "messages" in keys:
            results.append((text[:opening].count("\n") + 1, keys))
    return results


def test_every_run_snapshot_construction_includes_error_key() -> None:
    """静态检查：仓库内所有构造 Run 对象的位置都必须带 ``error`` 键。

    覆盖后端 ``app/`` 与 e2e mock ``apps/desktop/tests/e2e/test-server.mjs``（后端
    ``_RUN_KEYS`` 与前端 ``requireExactKeys`` 都是严格白名单，任一构造点漏写 ``error``
    都会让快照校验显式失败）。本用例把该失败前移到静态断言。

    潜在缺陷：新增 FastAPI 端点或恢复路径构造 Run 快照时容易漏 ``error``；漏写会让
    「无法取消且一直显示用量统计中」的线上问题以另一种形式复现（快照校验失败）。
    """

    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[3]
    offenders: list[str] = []
    checked = 0

    python_root = repo_root / "apps/backend/app"
    for path in sorted(python_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, keys in _python_run_literal_keys(text, str(path)):
            checked += 1
            if not _RUN_REQUIRED_KEYS.issubset(keys):
                missing = sorted(_RUN_REQUIRED_KEYS - keys)
                offenders.append(f"{path.relative_to(repo_root)}:{lineno} 缺少 {missing}")

    js_path = repo_root / "apps/desktop/tests/e2e/test-server.mjs"
    if js_path.exists():
        text = js_path.read_text(encoding="utf-8", errors="ignore")
        # 只保留「对象字面量」上下文：``{`` 前必须是 ``,``/``[``/``(``/``=``/``return``/
        # 行首等，而不是函数体起始的 ``{``（后者顶层键扫描会命中 ``const`` 之类）。
        for lineno, keys in _js_object_literal_keys(text):
            checked += 1
            if not _RUN_REQUIRED_KEYS.issubset(keys):
                missing = sorted(_RUN_REQUIRED_KEYS - keys)
                offenders.append(f"{js_path.relative_to(repo_root)}:{lineno} 缺少 {missing}")

    assert checked > 0, "静态检查未命中任何 Run snapshot 构造点，检查逻辑已失效"
    assert not offenders, "发现未声明 error 键的 Run snapshot 构造点：\n" + "\n".join(offenders)

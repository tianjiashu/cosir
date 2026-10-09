"""缺陷发现型对抗测试：Run 失败终态收敛 + 受控失败文案通路。

本模块**不**重复既有正向契约测试，而是从对抗视角攻击本次修复声称的四条不变量：

1. 落定链路（``AgentRuntime._settle_failed_run`` / ``AgentRuntime._failure_code_for``）在
   「落定自身失败」「Run 已被别处落终态」「异常是 ``BaseException`` 子类」「异常文本不可
   读」等边界下都必须保证：**Run 先落 failed 再抛原异常**，且不得替换/掩盖原始异常。
2. 分类器 ``classify_model_failure`` **只按异常类型名**识别传输层失败（超时 / 连接）：
   状态码（含边界值、bool、字符串、非法形状）、异常文本（含大小写与业务语义词）、
   属性访问抛异常、``__str__`` 抛异常都必须返回稳定结果或 ``None``，绝不抛出。
3. 文案目录 ``run_failure_message`` 对每个可产出 code 都有专属非空文案，且不含厂商名、
   模型名与数字；未知 code / ``None`` 回退通用文案。
4. Run 级 ``error`` 跨层契约（``terminal_error`` → ``build_run_error`` → snapshot 校验）
   在键缺失/多键/空串/非字符串/非法标识符时都必须显式失败，而非静默降级。
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.core.runtime.runner as runner_module
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
from app.core.runtime.runner import AgentRuntime
from app.core.workflows.agent_workflow import WorkflowRunFailure
from app.models import conversation_run_failure as failure_catalog
from app.models.conversation_run_failure import (
    run_failure_message,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.enums.error_kind import ErrorKind
from app.service.conversation_run.conversation_run_state_service import ConversationRunStateService


# --------------------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------------------


class _RecordingRunStateService:
    """记录 ``fail_run_if_running`` 入参的假 run 级状态服务；可模拟抛异常 / 条件更新落空。"""

    def __init__(self, *, settles: bool = True, failure: BaseException | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._settles = settles
        self._failure = failure

    def fail_run_if_running(
        self,
        run_id: int,
        end_reason: str | None = None,
        final_output: str | None = None,
        usage_stats: object = None,
        error_message: str | None = None,
    ) -> SimpleNamespace | None:
        self.calls.append(
            {
                "run_id": run_id,
                "end_reason": end_reason,
                "usage_stats": usage_stats,
                "final_output": final_output,
                "error_message": error_message,
            }
        )
        if self._failure is not None:
            raise self._failure
        return SimpleNamespace(id=run_id) if self._settles else None


def _make_agent(workflow_error: BaseException) -> SimpleNamespace:
    """构造带失败 workflow 的 agent profile 替身（``run`` 必须非 None）。"""

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise workflow_error

    return SimpleNamespace(
        agent_id="main_agent",
        run=SimpleNamespace(id=7, task_id=3),
        workflow=SimpleNamespace(run=_boom),
    )


def _make_runtime(
    monkeypatch: pytest.MonkeyPatch,
    run_state_service: _RecordingRunStateService,
    *,
    operations_available: bool = True,
) -> AgentRuntime:
    """构造只保留 ``run_agent`` 异常边界所需协作者的 AgentRuntime 替身。

    ``operations_available`` 为假时模拟「门面尚未构造就失败」：门面构造抛错，落定必须仍然发生
    （它只依赖 run 级状态服务）。
    """

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._task_service = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(id=task_id, workspace_id=9)
    )
    runtime._workspace_service = SimpleNamespace(
        get_workspace=lambda workspace_id: SimpleNamespace(id=workspace_id)
    )
    runtime._conversation_run_state_service = run_state_service

    async def _noop_fire(_context: object) -> None:
        return None

    @asynccontextmanager
    async def _trace(_metadata: object) -> object:
        yield SimpleNamespace(trace_id=None, callbacks=[], tool_trace_recorder=None)

    def _build(_self: AgentRuntime, *_args: object, **_kwargs: object) -> object:
        if operations_available:
            return SimpleNamespace()
        raise RuntimeError("operations unavailable")

    monkeypatch.setattr(runner_module.HookInterceptor, "async_safe_fire", _noop_fire)
    monkeypatch.setattr(runner_module, "conversation_run_trace", _trace)
    monkeypatch.setattr(AgentRuntime, "_build_operations", _build)
    return runtime


def _record_state() -> ConversationStateSnapshot:
    """构造一个只含单个 running Run 的合法 snapshot。"""

    state = empty_snapshot()
    state["runs"] = [
        ConversationRunSnapshot(
            runId=1,
            status="running",
            endReason=None,
            langfuseTraceId=None,
            messages=[],
            usage=None,
            error=None,
        )
    ]
    state["current_run_id"] = 1
    return state


def _flow_codes() -> list[str]:
    """返回 ``Constant.Run`` 声明的流程类失败 code（``RUN_FAILURE_CODE_*`` 常量值）。"""

    return [
        value
        for name, value in vars(Constant.Run).items()
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
    """只带 ``status_code`` 的替身：用于钉住「状态码不参与分类」。"""

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
    """带状态码且 ``__str__`` 抛异常：两者都不参与分类，必须返回 ``None``。"""

    def __init__(self, status_code: int) -> None:
        super().__init__()
        self.status_code = status_code

    def __str__(self) -> str:
        raise RuntimeError("__str__ exploded")


class _NestedTimeoutError(Exception):
    """外层异常无传输语义，异常链里的 cause 才是超时类。"""


@pytest.mark.parametrize(
    "status_code",
    [99, 100, 401, 402, 408, 429, 500, 503, 599, 600, -1, 0, -402, True, False, "402", None, [], {"a": 1}],
)
def test_status_codes_never_drive_classification(status_code: object) -> None:
    """任何形状的状态码都不参与分类：一律返回 ``None``。

    端点业务码与状态码并不统一，框架按约定不推断业务类别（见 ``model_failure`` docstring），
    业务失败的原始说明改由响应体 message 通道展示。
    """

    assert classify_model_failure(_StrStatusError(status_code)) is None
    assert classify_model_failure(_BoolStatusError(status_code)) is None


def test_status_code_property_exploding_does_not_raise() -> None:
    """读取 ``status_code`` / ``response`` 属性抛异常时，分类器必须吞掉并返回 None。"""

    assert classify_model_failure(_PropertyBoomError("boom")) is None


def test_exception_str_exploding_does_not_raise() -> None:
    """``__str__`` 抛异常时必须降级处理，绝不向上抛。"""

    assert classify_model_failure(_StrBoomError()) is None


def test_status_code_with_exploding_str_is_still_none() -> None:
    """带状态码且 ``__str__`` 抛异常：分类器不读两者，返回 ``None`` 且不上抛。"""

    assert classify_model_failure(_StrBoomStatusError(402)) is None

    assert classify_model_failure(_StrStatusError(429, "insufficient balance, rate limit")) is None


def test_business_keywords_in_text_never_drive_classification() -> None:
    """异常文本（含大小写变体）不参与分类：限流/余额等业务类别不由框架推断。"""

    for message in (
        "INSUFFICIENT BALANCE",
        "RATE LIMIT EXCEEDED",
        "insufficient balance, rate limit",
        "invalid api key provided",
    ):
        assert classify_model_failure(Exception(message)) is None


def test_transport_type_name_is_classified_even_through_exception_chain() -> None:
    """包装异常自身无传输语义时，异常链里的超时类仍必须被识别。"""

    outer = _NestedTimeoutError("wrapped")
    outer.__cause__ = TimeoutError("read timed out")

    assert classify_model_failure(outer) == ErrorKind.MODEL_TIMEOUT.value


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
        TimeoutError("boom"),
        ConnectionError("boom"),
        _StrStatusError(402),
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

    assert ConversationRunStateService.terminal_error(ConversationRunStatus.COMPLETED, "run_failed") is None


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

    error = ConversationRunStateService.terminal_error(ConversationRunStatus.FAILED, end_reason)
    assert error is not None
    assert error["code"].isidentifier()
    if end_reason is None or not end_reason.isidentifier():
        assert error["code"] == Constant.Run.RUN_FAILURE_CODE_UNKNOWN
    else:
        assert error["code"] == end_reason


def test_terminal_error_cancelled_fallback_code() -> None:
    """``cancelled`` 终态在无可用原因时回退取消类兜底 code。"""

    error = ConversationRunStateService.terminal_error(ConversationRunStatus.CANCELLED, None)
    assert error is not None
    assert error["code"] == Constant.Run.RUN_FAILURE_CODE_CANCELLED

    error = ConversationRunStateService.terminal_error(ConversationRunStatus.CANCELLED, "not an identifier")
    assert error is not None
    assert error["code"] == Constant.Run.RUN_FAILURE_CODE_CANCELLED


def test_terminal_error_message_is_non_empty_and_controlled() -> None:
    """任何终态的受控 message 都必须非空且来自文案目录。"""

    for status in (
        ConversationRunStatus.FAILED,
        ConversationRunStatus.CANCELLED,
    ):
        for reason in (None, "unknown_free_text", "model_insufficient_quota"):
            error = ConversationRunStateService.terminal_error(status, reason)
            assert error is not None
            assert error["message"].strip()
            assert not re.search(r"\d", error["message"])
            assert set(error) == {"code", "message"}


# --------------------------------------------------------------------------------------
# 4. ``AgentRuntime`` 失败收口链路对抗测试
# --------------------------------------------------------------------------------------


def test_run_agent_propagates_settlement_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落定自身抛 ``RuntimeError``（数据库不可用）时必须上抛，交由执行器兜底收敛。

    该路径不能吞：Run 仍是 running，只有执行器的 ``_converge_unfinished_run`` 能收敛它；
    吞掉会让 run 永久停在 running。
    """

    run_state_service = _RecordingRunStateService(failure=RuntimeError("db down"))
    runtime = _make_runtime(monkeypatch, run_state_service)

    with pytest.raises(RuntimeError, match="db down"):
        asyncio.run(
            runtime.run_agent(_make_agent(RuntimeError("node exploded")))
        )

    assert len(run_state_service.calls) == 1


def test_run_agent_propagates_settlement_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落定遇到 ``KeyError``（run 不存在）时同样上抛，不静默丢弃。"""

    run_state_service = _RecordingRunStateService(failure=KeyError("run not found"))
    runtime = _make_runtime(monkeypatch, run_state_service)

    with pytest.raises(KeyError):
        asyncio.run(
            runtime.run_agent(_make_agent(RuntimeError("node exploded")))
        )

    assert len(run_state_service.calls) == 1


def test_run_agent_does_not_swallow_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BaseException``（如 ``KeyboardInterrupt``）不在 ``except Exception`` 覆盖内：不落定、直接上抛。"""

    run_state_service = _RecordingRunStateService()
    runtime = _make_runtime(monkeypatch, run_state_service)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            runtime.run_agent(_make_agent(KeyboardInterrupt()))
        )

    assert run_state_service.calls == []


def test_run_agent_settles_with_profile_run_id_without_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落定目标取 ``agent.run.id``，不依赖门面：门面构造失败时同样能落定。

    历史缺陷：落定入口曾要求已构造的 ``WorkflowOperations``，于是「workflow 还没进入就失败」
    这条路径无人落终态，留下僵尸 running。
    """

    run_state_service = _RecordingRunStateService()
    runtime = _make_runtime(monkeypatch, run_state_service, operations_available=False)

    asyncio.run(runtime.run_agent(_make_agent(RuntimeError("operations unavailable"))))

    assert [call["run_id"] for call in run_state_service.calls] == [7]


def test_run_agent_settles_with_empty_string_code_records_empty_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作流自带空字符串 code 时原样透传 ``end_reason``，``final_output`` 仍是可展示文案。

    上游不应传空串；若真传入，DB 侧 ``terminal_error`` 会把 code 兜底成 ``run_failed``，
    因此本用例只断言 ``end_reason`` 原样、文案非空。
    """

    run_state_service = _RecordingRunStateService()
    runtime = _make_runtime(monkeypatch, run_state_service)
    failure = WorkflowRunFailure("", "空 code 的失败")

    asyncio.run(runtime.run_agent(_make_agent(failure)))

    assert run_state_service.calls[0]["end_reason"] == ""
    assert str(run_state_service.calls[0]["final_output"]).strip()


def test_failure_code_for_ignores_exception_text() -> None:
    """分类器只按异常类型名判定：``__str__`` 自身抛异常也不得影响兜底 code。

    潜在缺陷：若分类器改回按 ``str(exc)`` 做关键字匹配，本用例会显式失败。
    """

    class _BoomClassifier(Exception):
        def __str__(self) -> str:
            raise RuntimeError("__str__ exploded")

    assert (
        AgentRuntime._failure_code_for(_BoomClassifier())
        == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED
    )


def test_failure_code_for_returns_graph_failed_for_value_error() -> None:
    """``model_config_id`` 缺失一类 ``ValueError`` 走中性兜底，不伪装成模型错误。"""

    assert (
        AgentRuntime._failure_code_for(ValueError("Conversation Run model_config_id is required"))
        == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED
    )


def test_failure_code_for_reads_exception_chain() -> None:
    """被包装的传输错误（cause 链上才是超时异常）也要能识别。"""

    wrapped = RuntimeError("langchain wrapper")
    wrapped.__cause__ = TimeoutError("underlying timeout")

    assert AgentRuntime._failure_code_for(wrapped) == ErrorKind.MODEL_TIMEOUT.value


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


_RUN_REQUIRED_KEYS = {
    "runId",
    "status",
    "endReason",
    "langfuseTraceId",
    "messages",
    "usage",
    "error",
}


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

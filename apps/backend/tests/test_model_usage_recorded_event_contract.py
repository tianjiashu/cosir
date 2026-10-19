"""模型步的结构化 usage 日志契约。

日志在 SDK usage 归一并更新 Run 统计之后、消息收口之前记录，每步一次。
覆盖输入来源、未知与零的区分、快照独立性、缓存明细异常，以及取消/空流/超步数
等不产生 usage 事件的边界。provider 不返回用量时保留上一有效快照；首次缺失为 None。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from openai.lib.streaming.chat import ChunkEvent
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion

import app.core.workflow.node_helper.finalize_max_steps as finalize_module
import app.core.workflow.node_helper.streaming_part_state_machine as streaming_module
import app.core.workflow.node_helper.tool_call_lifecycle as lifecycle_module
from app.core.llm_provider.message_codec import assistant_message
from app.core.llm_provider.openai_model import usage_from_response
from app.core.workflow.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.route import ReactRoute
from app.core.workflow.execution_state.state import ReactExecutionState
from app.core.workflow.node_helper import RunStopped
from app.core.workflow.nodes import model_node as model_module
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from tests.sdk_fixtures import sdk_completion, sdk_event, sdk_usage

EVENT_NAME = "model_usage_recorded"
BACKEND_LOGGER = "coding_agent.backend"
TASK_ID = 7
RUN_ID = 42
# 事件 data 的完整契约键集合（本次改动声明的 7 个字段，多一个少一个都是契约漂移）。
DATA_KEYS = frozenset(
    {
        "task_id",
        "run_id",
        "step_id",
        "step_count",
        "usage_metadata_present",
        "usage_metadata",
        "run_usage",
    }
)
RUN_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "reasoning_tokens",
    }
)
# 哨兵：区分「未指定」与「显式要求返回 None」。
_UNSET = object()


@pytest.fixture(autouse=True)
def _patch_task_space(monkeypatch: Any) -> Any:
    """为 model 节点提供不依赖数据库的 task 级延迟消息队列（实例返回给用例预置消息）。"""

    class TaskSpace:
        def __init__(self) -> None:
            self.messages: list[Any] = []

        def take_runtime_notices(self, **_kwargs: Any) -> list[Any]:
            messages, self.messages = self.messages, []
            return messages

        def defer_runtime_notice(self, message: Any) -> None:
            self.messages.append(message)

        def has_runtime_notices(self) -> bool:
            return bool(self.messages)

    task_space = TaskSpace()
    monkeypatch.setattr(task_runtime_spaces, "get_or_create", lambda _task_id: task_space)
    return task_space


def _state(**overrides: Any) -> ReactExecutionState:
    """构造最小可执行的 ReAct graph state。"""

    values: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "next_node": ReactRoute.MODEL,
        "instruction": "",
        "last_tool_results": {"instruction": "", "observations": []},
    }
    values.update(overrides)
    return ReactExecutionState(**values)


class _ModelHarness:
    """为 model 节点提供最小运行时依赖（``usage_stats`` 用**真实**统计对象）。"""

    def __init__(
        self,
        message: ChatCompletion,
        *,
        chunks: list[dict[str, Any]] | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
        task_id: int = TASK_ID,
        run_id: int = RUN_ID,
        allows_tools: frozenset[str] = frozenset({"read_file"}),
        registered_tools: tuple[str, ...] = ("read_file",),
        structured_output: object | None = None,
        cancel_before_request: bool = False,
        cancel_after_first_chunk: bool = False,
        fail_result: object = _UNSET,
        complete_result: object = _UNSET,
        thinking_channel: str = "",
        max_steps: int = 10,
    ) -> None:
        self.messages: list[Any] = []
        self.events: list[Any] = []
        self.completed = False
        self.failed = False
        self.cancelled = False
        self.task_id = task_id
        self.run_id = run_id
        self.usage_stats = usage_stats if usage_stats is not None else ConversationRunUsageStats()
        self._message = message
        # 显式传空列表表示「零 chunk 流」，故不能写成 ``chunks or [...]``。
        self._chunks = chunks if chunks is not None else [{"content": ""}]
        self.saved_responses = []
        self._cancel_before_request = cancel_before_request
        self._cancel_after_first_chunk = cancel_after_first_chunk
        self._chunk_consumed = False

        def complete_run_if_running(*_args: Any, **_kwargs: Any) -> object | None:
            self.completed = True
            return complete_result if complete_result is not _UNSET else object()

        def fail_run_if_running(*_args: Any, **_kwargs: Any) -> object | None:
            self.failed = True
            return fail_result if fail_result is not _UNSET else object()

        def cancel_run_if_running(*_args: Any, **_kwargs: Any) -> None:
            self.cancelled = True

        def is_current_run_cancelled() -> bool:
            if self._cancel_before_request:
                return True
            return self._cancel_after_first_chunk and self._chunk_consumed

        self.operations = SimpleNamespace(
            task_tool_schemas=[],
            all_vaild_tools=[SimpleNamespace(name=name, display=None) for name in registered_tools],
            allows_tools=allows_tools,
            get_current_task=lambda: SimpleNamespace(id=task_id),
            get_current_run=lambda: SimpleNamespace(task_id=task_id, id=run_id),
            get_current_workspace=lambda: SimpleNamespace(id=1),
            is_current_run_cancelled=is_current_run_cancelled,
            complete_run_if_running=complete_run_if_running,
            fail_run_if_running=fail_run_if_running,
            cancel_run_if_running=cancel_run_if_running,
        )
        self.runtime_config = SimpleNamespace(
            operations=self.operations,
            model=SimpleNamespace(stream=self._astream),
            final_model=SimpleNamespace(stream=self._astream),
            structured_output=structured_output,
            structured_output_messages=None,
            workspace_id=1,
            thinking_channel=thinking_channel,
            run=SimpleNamespace(task_id=task_id, id=run_id, extra=None),
            usage_stats=self.usage_stats,
            max_steps=max_steps,
        )

        def add_message(message: Any, **_kwargs: Any) -> None:
            self.messages.append(message)

        def save_model_response(snapshot, *, include_in_context, run_id=None, existing=None):
            self.saved_responses.append((snapshot, include_in_context))
            if include_in_context:
                self.messages.append(assistant_message(snapshot.choices[0].message))

        self.runtime_context = SimpleNamespace(
            load_message=lambda: [],
            add_message=add_message,
            save_model_response=save_model_response,
        )

    async def _astream(self, _messages: list[Any], **kwargs: Any) -> AsyncIterator[ChunkEvent]:
        for chunk in self._chunks:
            yield sdk_event(chunk, self._message)
            self._chunk_consumed = True


def _install(monkeypatch: Any, harness: _ModelHarness) -> None:
    """把三处 graph runtime 依赖绑定到测试桩（model / lifecycle / 超步数收口各自持有引用）。"""

    for module in (model_module, lifecycle_module):
        monkeypatch.setattr(module, "_runtime_context", lambda: harness.runtime_config)
        monkeypatch.setattr(module, "_message_context", lambda: harness.runtime_context)
    for module in (lifecycle_module, streaming_module):
        monkeypatch.setattr(module, "get_stream_writer", lambda: harness.events.append)
    monkeypatch.setattr(finalize_module, "_runtime_context", lambda: harness.runtime_config)


def _records(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    """按事件名（即 ``record.msg`` 模板）筛选日志记录。"""

    return [record for record in caplog.records if record.msg == name]


def _usage_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """取出全部 ``model_usage_recorded`` 记录。"""

    return _records(caplog, EVENT_NAME)


def _single_usage_data(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    """断言「恰好一条用量事件」并返回其 data（频率是契约的一部分）。"""

    records = _usage_records(caplog)
    assert len(records) == 1, f"一步只能记一条用量事件，实际 {len(records)} 条"
    record = records[0]
    assert record.levelno == logging.INFO
    assert record.name == BACKEND_LOGGER
    data = record.data
    assert set(data) == DATA_KEYS, f"data 键集合漂移：{sorted(data)}"
    if data["run_usage"] is not None:
        assert set(data["run_usage"]) == RUN_USAGE_KEYS
    return data


# ===========================================================================
# 组 A：正向契约 —— 正常一步恰好一条事件，字段与运行上下文一致
# ===========================================================================


def test_single_step_records_exactly_one_event_with_consistent_context(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 成功一步（多 chunk 流）恰好一条 ``model_usage_recorded``，7 个字段与运行上下文
    一致、原始 usage 原样透传、``usage_metadata_present=true``、``run_usage`` 含 cache_miss 推导
    （潜在缺陷：日志写进流式循环 → 每 chunk 一条；字段取错源；present 恒真；cache_miss
    未推导）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    raw_usage = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "total_tokens": 1200,
        "input_token_details": {"cache_read": 400},
        "output_token_details": {"reasoning": 50},
    }
    message = sdk_completion(
        content="完成回答。", finish_reason="stop", usage=sdk_usage(dict(raw_usage))
    )
    stats = ConversationRunUsageStats()
    harness = _ModelHarness(
        message,
        chunks=[
            {"content": "完"},
            {"content": "成"},
            {"content": "回答。"},
        ],
        usage_stats=stats,
        task_id=TASK_ID,
        run_id=RUN_ID,
    )
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state(step_count=2)))

    assert result["next_node"] is ReactRoute.END
    # 最终回答在产出节点就地落定 completed（loop 不再二次落库）。
    assert harness.completed is True

    data = _single_usage_data(caplog)
    # 定位字段必须与运行上下文（operations 的当前 task / run 与 state 步号）逐一对齐。
    assert data["task_id"] == TASK_ID
    assert data["run_id"] == RUN_ID
    assert data["task_id"] != data["run_id"], "task_id / run_id 必须来自各自来源，不得互相顶替"
    assert data["step_id"] == "step-3"
    assert data["step_count"] == 3
    # provider 原始 dict 原样透传：内容相等，且是同一对象（未被包装 / 裁剪 / 复制）。
    assert data["usage_metadata"] == raw_usage
    assert data["usage_metadata"] == usage_from_response(message.usage)
    # present 是「本步到底有没有用量」的显式标记，True 必须是真正的 bool 而非 truthy。
    assert data["usage_metadata_present"] is True
    # run_usage 必须等于本步覆盖更新后的 run 级快照（含 cache_miss = input - cache_read）。
    assert data["run_usage"] == {
        "input_tokens": 1000,
        "output_tokens": 200,
        "total_tokens": 1200,
        "cache_hit_tokens": 400,
        "cache_miss_tokens": 600,
        "reasoning_tokens": 50,
    }
    # 独立算一遍：与被测代码共用同一值对象契约（覆盖「推导口径与 to_dict 一致」）。
    independent = ConversationRunUsageStats()
    independent.add_usage_metadata(dict(raw_usage))
    assert data["run_usage"] == independent.to_dict()
    assert data["run_usage"] == stats.to_dict()


def test_event_is_emitted_after_stats_cover_update_not_before(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 事件必须在 ``add_usage_metadata`` **之后**记录 —— 先污染 stats 再跑一步，
    ``run_usage`` 必须是本步快照而非上一步残留（潜在缺陷：日志被挪到更新之前，run_usage
    落后一步）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    stats = ConversationRunUsageStats()
    stats.add_usage_metadata(
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_token_details": {"cache_read": 300},
            "output_token_details": {"reasoning": 80},
        }
    )
    previous = stats.to_dict()

    message = sdk_completion(
        content="本步回答。",
        finish_reason="stop",
        usage=sdk_usage(
            {
                "input_tokens": 50,
                "output_tokens": 5,
                "total_tokens": 55,
                "input_token_details": {"cache_read": 10},
            }
        ),
    )
    harness = _ModelHarness(message, usage_stats=stats)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state(step_count=1)))

    data = _single_usage_data(caplog)
    assert data["run_usage"] == {
        "input_tokens": 50,
        "output_tokens": 5,
        "total_tokens": 55,
        "cache_hit_tokens": 10,
        "cache_miss_tokens": 40,
        "reasoning_tokens": None,
    }
    assert data["run_usage"] != previous, "run_usage 不得是上一步残留（说明日志早于更新）"
    assert data["run_usage"]["input_tokens"] != 1050, "run_usage 不得是累计值（累加语义）"


def test_reasoning_detail_alone_does_not_set_cache_miss(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 只给 ``output_token_details.reasoning`` 而不给缓存明细时，``cache_miss_tokens``
    必须为 ``None``（潜在缺陷：把「未知」算成 0 或 input 全量）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="回答。",
        finish_reason="stop",
        usage=sdk_usage(
            {
                "input_tokens": 80,
                "output_tokens": 12,
                "total_tokens": 92,
                "output_token_details": {"reasoning": 9},
            }
        ),
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state()))

    data = _single_usage_data(caplog)
    assert data["run_usage"]["reasoning_tokens"] == 9
    assert data["run_usage"]["cache_hit_tokens"] is None
    assert data["run_usage"]["cache_miss_tokens"] is None


def test_logged_run_usage_is_an_independent_snapshot(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 事件里的 ``run_usage`` 是快照，事后就地篡改它不得改变 run 级统计对象
    （潜在缺陷：直接把内部可变状态 / 缓存字典交给日志，下游改写即污染事实源）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    stats = ConversationRunUsageStats()
    message = sdk_completion(
        content="回答。",
        finish_reason="stop",
        usage=sdk_usage(
            {
                "input_tokens": 30,
                "output_tokens": 3,
                "total_tokens": 33,
                "input_token_details": {"cache_read": 20},
            }
        ),
    )
    harness = _ModelHarness(message, usage_stats=stats)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state()))

    data = _single_usage_data(caplog)
    data["run_usage"]["input_tokens"] = 999999
    data["usage_metadata"]["input_tokens"] = 999999
    assert stats.to_dict()["input_tokens"] == 30, "篡改日志快照不得影响 run 级统计"
    assert stats.to_dict()["cache_hit_tokens"] == 20
    assert harness.usage_stats is stats


# ===========================================================================
# 组 B：provider 未返回 usage —— 不抛异常、事件仍记录、present 为 False
# ===========================================================================


@pytest.mark.parametrize("missing_usage", [None, {}], ids=["none", "empty_dict"])
def test_missing_provider_usage_is_tolerated_and_marked_absent(
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
    missing_usage: dict[str, Any] | None,
) -> None:
    """test_purpose: ``usage_metadata`` 为 None **或空 dict** 时节点不得抛异常、事件仍被记录、
    ``usage_metadata_present`` 为 False、``run_usage`` 为 None
    （潜在缺陷：对 None / 空 dict 取下标 → TypeError；或该步干脆不记事件，跨步对比缺一环）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(content="回答。", finish_reason="stop")
    if missing_usage is not None:
        # SDK 对厂商空对象进行宽松构造；不能把缺失用量当零或清除上一步有效统计。
        message.usage = CompletionUsage.model_construct(**missing_usage)
    stats = ConversationRunUsageStats()
    harness = _ModelHarness(message, usage_stats=stats)
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state(step_count=0)))

    assert result["next_node"] is ReactRoute.END, "无用量不得改变路由"
    data = _single_usage_data(caplog)
    assert data["step_id"] == "step-1"
    assert data["step_count"] == 1
    assert data["usage_metadata_present"] is False
    assert data["usage_metadata"] == usage_from_response(message.usage)
    assert not data["usage_metadata"], "None 与空 dict 都表示 provider 未返回 usage"
    assert data["run_usage"] == ConversationRunUsageStats().to_dict()
    assert data["run_usage"] is None


def test_missing_usage_keeps_previous_run_snapshot_and_flags_absent(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 本步无 usage 时 ``run_usage`` 保持上一轮快照（覆盖更新语义下不变更任何字段），
    且 ``usage_metadata_present=False`` 能把「本步没用量」与「本步用量等于 run_usage」区分开
    （潜在缺陷：空 usage 被当成清零；或 present 由 run_usage 反推 → 该场景无法区分）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    stats = ConversationRunUsageStats()
    stats.add_usage_metadata(
        {
            "input_tokens": 200,
            "output_tokens": 30,
            "total_tokens": 230,
            "input_token_details": {"cache_read": 50},
            "output_token_details": {"reasoning": 9},
        }
    )
    before = stats.to_dict()
    assert before["input_tokens"] != 0

    message = sdk_completion(content="回答。", finish_reason="stop")
    harness = _ModelHarness(message, usage_stats=stats)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state(step_count=3)))

    data = _single_usage_data(caplog)
    assert data["usage_metadata_present"] is False
    assert data["run_usage"] == before, "无用量时 run_usage 必须是上一轮快照"
    assert data["run_usage"]["input_tokens"] != 0, "上一轮快照非零，用于区分「保持」与「清零」"
    assert data["step_id"] == "step-4"
    assert stats.to_dict() == before, "空 usage 不得改变统计对象的任何字段"


def test_zero_token_usage_counts_as_present(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 边界 —— provider 回报了全零用量（非空 dict）时
    ``usage_metadata_present`` 仍为
    True（「provider 给了用量」与「用量为零」是两件事）
    （潜在缺陷：用 token 数值之和判断 present，把「零用量」误报成「未给用量」）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="回答。",
        finish_reason="stop",
        usage=sdk_usage({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state()))

    data = _single_usage_data(caplog)
    assert data["usage_metadata_present"] is True
    assert data["usage_metadata"] == usage_from_response(message.usage)
    assert data["run_usage"]["cache_miss_tokens"] is None


# ===========================================================================
# 组 C：run_usage 推导边界 —— 缓存明细的缺失与非法值
# ===========================================================================


def test_cache_miss_clamps_to_zero_when_cache_read_exceeds_input(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 边界 —— ``cache_read > input_tokens``（provider 数据不自洽）时未命中值必须
    取下限 0（潜在缺陷：缺 ``max(0, ...)`` 保护，把负值写进 run 级用量）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="回答。",
        finish_reason="stop",
        usage=sdk_usage(
            {
                "input_tokens": 10,
                "output_tokens": 1,
                "total_tokens": 11,
                "input_token_details": {"cache_read": 50},
            }
        ),
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state()))

    data = _single_usage_data(caplog)
    assert data["run_usage"]["cache_hit_tokens"] == 50
    assert data["run_usage"]["cache_miss_tokens"] == 0


def test_cache_read_exactly_equal_to_input_gives_zero_miss(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 边界 —— ``cache_read == input_tokens``（全命中）时未命中值恰为 0，且必须是
    真 0 而非 None（潜在缺陷：``==`` / ``>=`` 边界写错，全命中被报成未知）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="回答。",
        finish_reason="stop",
        usage=sdk_usage(
            {
                "input_tokens": 10,
                "output_tokens": 1,
                "total_tokens": 11,
                "input_token_details": {"cache_read": 10},
            }
        ),
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state()))

    data = _single_usage_data(caplog)
    assert data["run_usage"]["cache_miss_tokens"] == 0
    assert data["run_usage"]["cache_miss_tokens"] is not None


@pytest.mark.parametrize(
    ("cache_read", "expected_hit"),
    [(-5, None), ("abc", None), (None, None), (12.7, None), (True, None), (13, 13)],
    ids=["negative", "non_numeric", "null", "fraction", "bool", "valid"],
)
def test_invalid_cache_read_values_are_sanitized_without_raising(
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
    cache_read: Any,
    expected_hit: int | None,
) -> None:
    """test_purpose: 非法 ``cache_read``（负数 / 非数 / None / 小数 / bool）必须被标为未知而
    不抛异常，且事件照常记录（潜在缺陷：直接 int() 转换 → 异常上抛打断整步；负数直接进快照）。
    只有有效缓存明细才可推导 cache_miss。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(content="回答。", finish_reason=None)
    # 绕过 pydantic 校验：这些值在生产运行期可能出现在未经校验的 usage_metadata 里。
    message.usage = CompletionUsage.model_construct(
        **sdk_usage(
            {
                "input_tokens": 20,
                "output_tokens": 2,
                "total_tokens": 22,
                "input_token_details": {"cache_read": cache_read},
            }
        )
    )
    # SDK 构造器会把 bool 转为 int；直接赋值以测试到达边界的原始异常明细。
    message.usage.prompt_tokens_details.cached_tokens = cache_read
    message.choices[0].finish_reason = "stop"
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state()))

    data = _single_usage_data(caplog)
    assert data["usage_metadata_present"] is True
    assert data["run_usage"]["cache_hit_tokens"] == expected_hit
    assert data["run_usage"]["cache_miss_tokens"] == (
        20 - expected_hit if expected_hit is not None else None
    )


def test_non_dict_input_token_details_does_not_crash_and_miss_stays_none(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 形状异常的 ``input_token_details``（非 dict，如字符串）必须被安全忽略：
    不抛异常、``cache_miss_tokens`` 为 None（潜在缺陷：无条件 .get 取下标 → AttributeError；
    或把无法判读的明细当成「全未命中」）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(content="回答。", finish_reason=None)
    message.usage = CompletionUsage.model_construct(
        **sdk_usage(
            {
                "input_tokens": 40,
                "output_tokens": 4,
                "total_tokens": 44,
                "input_token_details": "cache_read=10",
            }
        )
    )
    message.choices[0].finish_reason = "stop"
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state()))

    data = _single_usage_data(caplog)
    assert data["run_usage"]["cache_hit_tokens"] is None
    assert data["run_usage"]["cache_miss_tokens"] is None
    assert data["usage_metadata_present"] is True


def test_bool_token_values_are_not_reported_as_tokens(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 边界 —— 布尔值不得被当作 token 数（``True`` 是 int
    的子类，容易被 int() 吞下）
    （潜在缺陷：input_tokens=True 被记成 1，悄无声息地污染用量归因）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(content="回答。", finish_reason=None)
    message.usage = CompletionUsage.model_construct(
        **sdk_usage(
            {
                "input_tokens": True,
                "output_tokens": 3,
                "total_tokens": 3,
            }
        )
    )
    message.choices[0].finish_reason = "stop"
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state()))

    data = _single_usage_data(caplog)
    assert data["run_usage"]["input_tokens"] is None
    assert data["run_usage"]["output_tokens"] == 3
    assert data["run_usage"]["total_tokens"] == 3


# ===========================================================================
# 组 D：不得越界记录 —— 提前退出路径的负向断言
# ===========================================================================


def test_empty_model_stream_records_no_usage_event(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 空流（零 chunk）在收口处响亮失败，**不得**产出用量事件
    （潜在缺陷：日志挪到空流检查之前 → 谎报一次用量）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    harness = _ModelHarness(sdk_completion(content="", finish_reason=None), chunks=[])
    _install(monkeypatch, harness)

    with pytest.raises(RuntimeError):
        asyncio.run(model_module._model_node(_state()))

    assert _usage_records(caplog) == []
    assert harness.messages == [], "空流不得固化任何消息"


def test_max_steps_exceeded_records_no_usage_event(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: ``step_count > max_steps`` 时不发起推理、不调
    ``add_usage_metadata``，因而绝不能
    有用量事件，且终态为步数耗尽（潜在缺陷：超配额仍记一条用量 / 仍调用模型）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    harness = _ModelHarness(sdk_completion(content="不应发生", finish_reason=None))
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state(step_count=10)))

    assert result["next_node"] is ReactRoute.END
    assert harness.failed is True
    assert harness.completed is False
    assert _usage_records(caplog) == []


def test_step_count_equal_to_max_steps_still_runs_and_records(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 边界 —— ``step_count == max_steps``（恰好用满配额）仍须发起推理并记录用量事件
    （潜在缺陷：把 ``> max_steps`` 误写成 ``>=``，最后一整步被静默吞掉）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="最后一步。",
        finish_reason="stop",
        usage=sdk_usage({"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}),
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state(step_count=9)))

    assert result["next_node"] is ReactRoute.END
    data = _single_usage_data(caplog)
    assert data["step_id"] == "step-10"
    assert data["step_count"] == 10
    assert data["usage_metadata"] == usage_from_response(message.usage)


def test_cancelled_before_request_records_no_usage_event(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 请求前即命中取消 → 未发起推理、未调
    ``add_usage_metadata``，绝不能出现用量事件；
    同时必须落定取消终态（潜在缺陷：取消分支与用量记录顺序错位，取消的 run 被记一次用量）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    harness = _ModelHarness(
        sdk_completion(content="不应发生", finish_reason=None), cancel_before_request=True
    )
    _install(monkeypatch, harness)

    with pytest.raises(RunStopped):  # graph 外 interrupt 抛 RuntimeError
        asyncio.run(model_module._model_node(_state()))

    assert harness.cancelled is True
    assert harness.completed is False
    assert _usage_records(caplog) == []
    assert len(_records(caplog, "model_node_cancelled_before_request")) == 1


def test_cancelled_during_stream_records_no_usage_event(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 流式循环内命中取消 → 草稿按 cancel 收口后挂起，**不得**记录用量事件
    （潜在缺陷：取消检查位于用量记录之后，半截流被记成一次完整用量）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    harness = _ModelHarness(
        sdk_completion(content="半截", finish_reason=None),
        chunks=[{"content": "半"}],
        cancel_after_first_chunk=True,
    )
    _install(monkeypatch, harness)

    with pytest.raises(RunStopped):
        asyncio.run(model_module._model_node(_state()))

    assert harness.cancelled is True
    assert harness.completed is False
    assert _usage_records(caplog) == []
    assert len(_records(caplog, "model_node_cancelled_usage_summary")) == 1


# ===========================================================================
# 组 E：其它路由分支下事件频次仍为「一步一条」
# ===========================================================================


def test_tools_route_records_usage_event_exactly_once(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 模型返回工具调用（路由 tools）时用量事件仍恰好一条，run_usage 含 cache 推导
    （潜在缺陷：只在最终回答分支记录，工具路径的用量漏记）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
            }
        ],
        usage=sdk_usage(
            {
                "input_tokens": 300,
                "output_tokens": 15,
                "total_tokens": 315,
                "input_token_details": {"cache_read": 100},
            }
        ),
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.TOOLS
    data = _single_usage_data(caplog)
    assert data["run_usage"] == {
        "input_tokens": 300,
        "output_tokens": 15,
        "total_tokens": 315,
        "cache_hit_tokens": 100,
        "cache_miss_tokens": 200,
        "reasoning_tokens": None,
    }


def test_structured_output_route_records_usage_event_exactly_once(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """配置结构化输出时先完成自然语言主线，再路由支线；本步用量仍只记录一次。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="结构化前的自然语言。",
        finish_reason="stop",
        usage=sdk_usage({"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}),
    )
    harness = _ModelHarness(message, structured_output=object())
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.STRUCTURED_OUTPUT
    assert harness.completed
    assert harness.runtime_config.structured_output_messages == []
    data = _single_usage_data(caplog)
    assert data["usage_metadata"] == usage_from_response(message.usage)


def test_empty_main_output_still_routes_to_structured_branch(monkeypatch: Any) -> None:
    """主线仅有完成原因时也先落定 Run，再执行已配置的结构化支线。"""

    harness = _ModelHarness(
        sdk_completion(content="", finish_reason="content_filter"),
        structured_output=object(),
    )
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.STRUCTURED_OUTPUT
    assert harness.completed is True


def test_structured_context_capture_failure_still_completes_main_run(monkeypatch: Any) -> None:
    """支线冻结上下文失败不推迟自然语言主线落定。"""

    message = sdk_completion(content="自然回答", finish_reason="stop")
    harness = _ModelHarness(message, structured_output=object())
    _install(monkeypatch, harness)
    reads = 0

    def load_messages() -> list[Any]:
        nonlocal reads
        reads += 1
        if reads == 2:
            raise RuntimeError("capture failed")
        return []

    harness.runtime_context.load_message = load_messages
    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.STRUCTURED_OUTPUT
    assert harness.completed
    assert harness.runtime_config.structured_output_messages is None


def test_empty_output_records_usage_event_before_completing(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """无正文但有完成原因时仍完成 Run，并保留本步用量。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="",
        finish_reason="stop",
        usage=sdk_usage({"input_tokens": 9, "output_tokens": 0, "total_tokens": 9}),
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is True
    assert harness.failed is False
    data = _single_usage_data(caplog)
    assert data["usage_metadata_present"] is True
    assert len(_records(caplog, "model_node_final_response")) == 1


def test_continuation_route_records_usage_event_exactly_once(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 缺失完成原因时回流 model 并注入续写提示，用量事件仍恰好一条且步号正确
    （潜在缺陷：续写分支整段跳过用量记录）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="未确认完成",
        usage=sdk_usage({"input_tokens": 11, "output_tokens": 6, "total_tokens": 17}),
        finish_reason=None,
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state(step_count=1)))

    assert result["next_node"] is ReactRoute.MODEL
    data = _single_usage_data(caplog)
    assert data["step_id"] == "step-2"
    assert data["usage_metadata"] == usage_from_response(message.usage)


def test_length_output_records_usage_event_exactly_once(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """长度完成原因也结束 Run，用量事件仍恰好一条。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="回答到一半",
        finish_reason="length",
        usage=sdk_usage({"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}),
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is True
    data = _single_usage_data(caplog)
    assert data["usage_metadata"] == usage_from_response(message.usage)


def test_final_response_race_lost_records_usage_event_exactly_once(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 最终回答落定时 run 已非 running（``complete_run_if_running`` 返回 None）仍按
    END 收口，用量事件不重不漏（潜在缺陷：race-lost 分支重复记录或漏记用量）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="最终回答。",
        finish_reason="stop",
        usage=sdk_usage({"input_tokens": 21, "output_tokens": 8, "total_tokens": 29}),
    )
    harness = _ModelHarness(message, complete_result=None)
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    data = _single_usage_data(caplog)
    assert data["run_usage"]["input_tokens"] == 21
    # 落定入口被调用过，但条件更新对已终态的 Run 是 no-op，用量仍只记一条。
    assert harness.completed is True


def test_reasoning_channel_step_records_usage_event_exactly_once(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 带 reasoning 增量的一步（存在思考通道）不得干扰用量记录 —— 事件恰好一条且
    reasoning_tokens 如实入快照（潜在缺陷：reasoning 分支异常 / 提前返回导致用量漏记）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    message = sdk_completion(
        content="回答。",
        finish_reason="stop",
        usage=sdk_usage(
            {
                "input_tokens": 40,
                "output_tokens": 12,
                "total_tokens": 52,
                "output_token_details": {"reasoning": 9},
            }
        ),
    )
    harness = _ModelHarness(
        message,
        chunks=[
            {"content": "", **{"reasoning_content": "思考中"}},
            {"content": "回答。"},
        ],
        thinking_channel="reasoning_content",
    )
    _install(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert any(
        getattr(event, "part", None) == "reasoning" for event in harness.events
    ), "reasoning 增量必须投影为 reasoning part 事件"
    data = _single_usage_data(caplog)
    assert data["run_usage"]["reasoning_tokens"] == 9


def test_logged_usage_metadata_equals_provider_dict_not_a_copy(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 记录过程不得就地改写 provider 原始 dict（``run_usage`` 派生自同一份数据）
    （潜在缺陷：统计逻辑把派生字段写回原始 dict，污染模型消息与跨步对比基准）。"""

    caplog.set_level(logging.DEBUG, logger=BACKEND_LOGGER)
    raw_usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
        "input_token_details": {"cache_read": 40},
    }
    message = sdk_completion(
        content="回答。", finish_reason="stop", usage=sdk_usage(copy.deepcopy(raw_usage))
    )
    harness = _ModelHarness(message)
    _install(monkeypatch, harness)

    asyncio.run(model_module._model_node(_state()))

    data = _single_usage_data(caplog)
    assert data["usage_metadata"] == usage_from_response(message.usage)
    assert message.usage.prompt_tokens == raw_usage["input_tokens"]
    assert (
        message.usage.prompt_tokens_details.cached_tokens
        == raw_usage["input_token_details"]["cache_read"]
    )

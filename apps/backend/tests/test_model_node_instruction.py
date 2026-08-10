"""``model`` 节点单测：模型同时产出文本 + 工具调用时，说明文本必须作为 instruction 下传。

覆盖第零铁律校准的语义：ReAct 中「边说明边调工具」合法，模型说明文本虽不计入最终回复，
但必须随 ``pending_tool_calls`` 的 ``instruction`` 键下传给 ``tools`` / ``observe`` 节点，
使下游执行与错误排查能看到模型意图。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from app.core.workflows.nodes import model_node as model_mod
from app.core.workflows.react.state import ReactGraphState


def _state(**overrides: Any) -> ReactGraphState:
    base: dict[str, Any] = {
        "step_count": 0,
        "tool_error_count": 0,
        "requested_tool": False,
        "final_response": False,
        "terminal": False,
        "pending_tool_calls": [],
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": [],
    }
    base.update(overrides)
    return ReactGraphState(**base)


def _fake_runtime_config(*, model_chunks: list[AIMessageChunk]) -> SimpleNamespace:
    """构造可被 model 节点消费的伪 RuntimeConfig。

    ``model_chunks`` 为 model.astream 将产出的一系列 AIMessageChunk；空列表模拟
    模型无输出。``operations`` / ``turn`` / ``usage_stats`` 等均为最小桩。
    """

    async def _astream(_messages: Any) -> Any:
        for chunk in model_chunks:
            yield chunk

    model = SimpleNamespace(astream=_astream)
    turn = SimpleNamespace(turn_id="t1")
    operations = SimpleNamespace(
        is_current_turn_cancelled=lambda: False,
        append_runtime_message=lambda *_a, **_k: None,
        fail_turn_if_running=lambda *_a, **_k: SimpleNamespace(turn_id="t1"),
        complete_turn_if_running=lambda *_a, **_k: SimpleNamespace(turn_id="t1"),
        get_current_task=lambda: SimpleNamespace(task_id="task-1"),
        get_current_turn=lambda: turn,
    )
    usage_stats = SimpleNamespace(
        add_message_usage=lambda _u: None,
        to_dict=lambda: {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 1,
            "reasoning_tokens": 0,
        },
    )
    return SimpleNamespace(
        operations=operations,
        turn=turn,
        model=model,
        usage_stats=usage_stats,
        langfuse_trace_id="trace-1",
        start_time=0.0,
    )


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, cfg: SimpleNamespace) -> None:
    monkeypatch.setattr(model_mod, "_runtime_config", lambda: cfg)
    monkeypatch.setattr(model_mod, "_runtime_context", lambda: SimpleNamespace(
        load_message=lambda: [],
        add_message=lambda _m: None,
    ))
    monkeypatch.setattr(model_mod, "write_event", lambda *a, **k: None)


def _text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _tool_chunk(tool_name: str, args: dict[str, Any], call_id: str) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": tool_name, "args": args, "id": call_id}],
    )


async def test_text_and_tool_calls_carries_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型同时产出文本与工具调用：说明文本必须作为 instruction 下传每个 call。"""
    chunks = [
        _text_chunk("我先用 grep 查一下文件结构"),
        _tool_chunk("search_files", {"pattern": "*.py"}, "call-1"),
    ]
    cfg = _fake_runtime_config(model_chunks=chunks)
    _patch_runtime(monkeypatch, cfg)
    state = _state()
    result = await model_mod._model_node(state)
    assert result["requested_tool"] is True
    assert len(result["pending_tool_calls"]) == 1
    call = result["pending_tool_calls"][0]
    assert call["tool_name"] == "search_files"
    assert call["call_id"] == "call-1"
    assert call["instruction"] == "我先用 grep 查一下文件结构"


async def test_multiple_tool_calls_share_same_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一批多个工具调用共享同一段说明文本（ReAct 中说明是本轮动作总述）。"""
    chunks = [
        _text_chunk("先做两件事"),
        _tool_chunk("search_files", {"pattern": "a"}, "call-1"),
        _tool_chunk("read_file", {"path": "b.py"}, "call-2"),
    ]
    cfg = _fake_runtime_config(model_chunks=chunks)
    _patch_runtime(monkeypatch, cfg)
    result = await model_mod._model_node(_state())
    assert len(result["pending_tool_calls"]) == 2
    for call in result["pending_tool_calls"]:
        assert call["instruction"] == "先做两件事"


async def test_pure_tool_calls_no_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯工具调用（无文本）：instruction 缺省为空串，向后兼容。"""
    chunks = [_tool_chunk("read_file", {"path": "b.py"}, "call-1")]
    cfg = _fake_runtime_config(model_chunks=chunks)
    _patch_runtime(monkeypatch, cfg)
    result = await model_mod._model_node(_state())
    assert result["pending_tool_calls"][0]["instruction"] == ""


async def test_instruction_logged_in_tool_branch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """进入工具分支时日志必须记录 has_instruction 与 instruction_length，便于排查。"""
    import logging

    chunks = [
        _text_chunk("说明文本"),
        _tool_chunk("search_files", {"pattern": "*.py"}, "call-1"),
    ]
    cfg = _fake_runtime_config(model_chunks=chunks)
    _patch_runtime(monkeypatch, cfg)
    with caplog.at_level(logging.INFO):
        await model_mod._model_node(_state())
    logged = [r for r in caplog.records if "model_node_tool_branch" in r.message]
    assert logged
    extra = getattr(logged[0], "data", None)
    assert extra is not None
    assert extra["has_instruction"] is True
    assert extra["instruction_length"] == len("说明文本")

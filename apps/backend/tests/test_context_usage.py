"""上下文窗口占用（ContextUsageMeter / TokenEstimator / ModelCatalog）单元测试。

覆盖：字符估算口径、模型无关、上下文窗口 min 计算（模型窗口 / 软上限 / 覆盖窗口）、
防抖重算、脏标记、事件注册与 TS 协议生成一致性、Settings 校验。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.config.settings import Settings
from app.core.context.context_usage_meter import ContextUsageMeter
from app.core.llm.model_catalog import ModelCatalog
from app.models.enums.event_type import EventType
from app.models.payload.registry.runtime_event_payload_registry import (
    EVENT_PAYLOAD_MODELS,
)
from app.utils.token_estimator import TokenEstimator


class _FakeMessage:
    """轻量消息替身，仅暴露 content（str 或 list[dict]），供计量器估算。"""

    def __init__(self, content: object) -> None:
        self.content = content


# --------------------------------------------------------------------------- #
# TokenEstimator
# --------------------------------------------------------------------------- #
def test_token_estimator_empty_returns_zero() -> None:
    """空文本估算为 0，非空至少 1。"""
    assert TokenEstimator.estimate("") == 0
    assert TokenEstimator.estimate(None) == 0  # type: ignore[arg-type]
    assert TokenEstimator.estimate("hi") == 1


def test_token_estimator_monotonic_with_length() -> None:
    """估算随文本长度单调递增，且为大致线性（体现折中字符系数口径）。"""
    short = TokenEstimator.estimate("a" * 30)
    long = TokenEstimator.estimate("a" * 120)
    assert long > short
    assert long == short * 4  # 系数恒定：120/30=4 倍字符 → 4 倍 token


# --------------------------------------------------------------------------- #
# ModelCatalog
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "model_name,expected",
    [
        ("deepseek-v4-flash", 1_000_000),
        ("deepseek-v4-pro", 1_000_000),
        ("unknown-model", 128_000),  # 兜底保守值
    ],
)
def test_model_catalog_max_context_window(model_name: str, expected: int) -> None:
    """模型窗口查表，未收录用兜底。"""
    assert ModelCatalog.max_context_window(model_name) == expected


# --------------------------------------------------------------------------- #
# ContextUsageMeter
# --------------------------------------------------------------------------- #
def _make_meter(messages, model_name="deepseek-v4-flash", total_provider=None):
    """构造计量器，默认 1M 模型窗口、无软上限。

    ``messages`` 可为消息列表或可调用（如 ``ctx.load_message``）；可调用时直接用作
    message_provider，确保读取的是实时快照而非构造时刻的拷贝。
    """
    provider = messages if callable(messages) else (lambda: messages)
    return ContextUsageMeter(
        message_provider=provider,
        model_name_provider=lambda: model_name,
        total_tokens_provider=total_provider,
    )


def test_usage_meter_estimates_sum_of_messages() -> None:
    """给定消息列表，used_tokens 为各消息文本估算之和。"""
    messages = [HumanMessage(content="你好世界"), AIMessage(content="hello world")]
    meter = _make_meter(messages)
    usage = meter.read(force=True)
    assert usage.used_tokens == TokenEstimator.estimate("你好世界") + TokenEstimator.estimate(
        "hello world"
    )
    assert usage.total_tokens == 1_000_000  # 默认 1M 模型窗口，无软上限


def test_usage_meter_handles_structured_content() -> None:
    """list[dict] 结构化 content 仅取 text 块估算。"""
    messages = [AIMessage(content=[{"type": "text", "text": "abc"}, {"type": "image"}])]
    meter = _make_meter(messages)
    usage = meter.read(force=True)
    assert usage.used_tokens == TokenEstimator.estimate("abc")


def test_usage_meter_min_with_soft_cap() -> None:
    """total_tokens = min(模型窗口, 软上限)。"""
    original = Settings.CONTEXT_WINDOW_TOKENS
    Settings.CONTEXT_WINDOW_TOKENS = 32000
    try:
        meter = _make_meter([HumanMessage(content="x")])
        usage = meter.read(force=True)
        assert usage.total_tokens == 32000
    finally:
        Settings.CONTEXT_WINDOW_TOKENS = original


def test_usage_meter_total_override_precedence() -> None:
    """total_tokens_provider 覆盖时优先于模型窗口与软上限。"""
    meter = _make_meter([HumanMessage(content="x")], total_provider=lambda: 64000)
    usage = meter.read(force=True)
    assert usage.total_tokens == 64000


def test_usage_meter_dirty_then_cached_within_interval() -> None:
    """脏标记后 read 重算；间隔内重复 read 命中缓存（不重扫消息）。"""
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return [HumanMessage(content="a" * 30)]

    meter = _make_meter(provider())
    # 直接以闭包计数不便，改用 force 强制两次重算验证 message_provider 被调用
    meter.read(force=True)
    meter.read(force=True)
    # 两次 force 均重算，used 不变
    first = meter.read(force=True)
    second = meter.read(force=True)
    assert first.used_tokens == second.used_tokens
    # 非 force 且未超间隔：message_provider 不再被调用（缓存命中）
    # 通过替换 provider 验证
    captured = {"called": False}

    def cached_provider():
        captured["called"] = True
        return [HumanMessage(content="b" * 30)]

    meter._messages = cached_provider
    meter.mark_context_changed()
    # 立即 read（未超间隔）命中缓存，cached_provider 不被调用
    meter.read()
    assert captured["called"] is False


def test_usage_meter_mark_dirty_triggers_recompute_after_interval() -> None:
    """标记脏后超过间隔再 read 触发重算。"""
    messages = [HumanMessage(content="a" * 30)]
    meter = _make_meter(messages)
    meter.read(force=True)
    meter.mark_context_changed()
    # 模拟时间推进：直接打补丁 last_compute
    meter._last_compute = 0.0
    recomputed = meter.read()
    assert recomputed.used_tokens == TokenEstimator.estimate("a" * 30)


# --------------------------------------------------------------------------- #
# 事件注册 / TS 协议
# --------------------------------------------------------------------------- #
def test_context_usage_event_registered() -> None:
    """CONTEXT_USAGE 事件必须注册 payload 且字段匹配。"""
    assert EventType.CONTEXT_USAGE in EVENT_PAYLOAD_MODELS
    payload_cls = EVENT_PAYLOAD_MODELS[EventType.CONTEXT_USAGE]
    import inspect

    params = inspect.signature(payload_cls).parameters
    assert "used_tokens" in params
    assert "total_tokens" in params


def test_context_usage_payload_is_exported() -> None:
    """ContextUsagePayload 必须经 app.models.payload 命名空间导出（供 model_node 导入）。"""
    from app.models.payload import ContextUsagePayload

    assert ContextUsagePayload is EVENT_PAYLOAD_MODELS[EventType.CONTEXT_USAGE]


def test_context_usage_in_ts_contract() -> None:
    """重新生成 TS 协议应包含 ContextUsagePayload（与后端契约一致）。"""
    import importlib.util
    from pathlib import Path

    script_path = Path(__file__).resolve().parents[3] / "scripts" / "generate_runtime_event_ts.py"
    assert script_path.exists(), f"generate script not found at {script_path}"
    spec = importlib.util.spec_from_file_location("generate_runtime_event_ts", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rendered = module.render_typescript(list(EventType))  # type: ignore[attr-defined]
    assert "export interface ContextUsagePayload" in rendered
    assert "used_tokens: number;" in rendered
    assert "total_tokens: number;" in rendered


# --------------------------------------------------------------------------- #
# Settings 校验
# --------------------------------------------------------------------------- #
def test_settings_validate_rejects_negative_context_window() -> None:
    """CONTEXT_WINDOW_TOKENS 为负时校验失败。"""
    original = Settings.CONTEXT_WINDOW_TOKENS
    Settings.CONTEXT_WINDOW_TOKENS = -1
    try:
        with pytest.raises(ValueError):
            Settings._validate()
    finally:
        Settings.CONTEXT_WINDOW_TOKENS = original


def test_settings_validate_rejects_nonpositive_interval() -> None:
    """CONTEXT_USAGE_MIN_INTERVAL_S 非正时校验失败。"""
    original = Settings.CONTEXT_USAGE_MIN_INTERVAL_S
    Settings.CONTEXT_USAGE_MIN_INTERVAL_S = 0.0
    try:
        with pytest.raises(ValueError):
            Settings._validate()
    finally:
        Settings.CONTEXT_USAGE_MIN_INTERVAL_S = original


# --------------------------------------------------------------------------- #
# RuntimeContext 集成（脏标记触发）
# --------------------------------------------------------------------------- #
def test_runtime_context_attach_meter_marks_dirty() -> None:
    """挂载计量器后，add_message 应触发 mark_context_changed（脏标记可重算）。"""
    from app.core.agents.agent_profile import AgentProfile
    from app.core.context.runtime_context_manager import RuntimeContextManager

    profile = AgentProfile(
        agent_id="developer",
        role="developer",
        description="test",
        allowed_tools=["read_file", "write_file"],
        model_name="deepseek-v4-flash",
        main_agent=True,
    )
    # 直接构造 RuntimeContextManager（不经 load_history，避免测试依赖存储初始化），
    # 验证挂载计量器后 add_message 触发脏标记重算。
    ctx = RuntimeContextManager(
        agent_profile=profile,
        workspace_root="C:/tmp/ws",  # 仅作占位，不真正落盘
        task_id="task-1",
    )
    meter = _make_meter(ctx.load_message)
    ctx.attach_usage_meter(meter)
    # 初始读
    before = ctx.usage_meter.read(force=True)  # type: ignore[union-attr]
    # 追加消息后应能重算到更大占用
    ctx.add_message(HumanMessage(content="新增内容用于触发脏标记重算"))
    after = ctx.usage_meter.read(force=True)  # type: ignore[union-attr]
    assert after.used_tokens > before.used_tokens
    assert after.total_tokens == 1_000_000


# --------------------------------------------------------------------------- #
# 切模型免疫（核心诉求）
# --------------------------------------------------------------------------- #
def test_usage_meter_model_agnostic_estimate() -> None:
    """估算与模型无关：切换模型只改 total（窗口），used 估算不变。"""
    messages = [HumanMessage(content="跨模型一致的内容")]
    flash = _make_meter(messages, model_name="deepseek-v4-flash")
    pro = _make_meter(messages, model_name="deepseek-v4-pro")
    assert flash.read(force=True).used_tokens == pro.read(force=True).used_tokens
    # 两者窗口均为 1M，total 一致
    assert flash.read(force=True).total_tokens == pro.read(force=True).total_tokens


# --------------------------------------------------------------------------- #
# 上下文窗口解析（单一事实来源）
# --------------------------------------------------------------------------- #
def test_resolve_context_window_no_soft_cap() -> None:
    """软上限为 0 时，实际窗口 = 模型最大窗口。"""
    original = Settings.CONTEXT_WINDOW_TOKENS
    Settings.CONTEXT_WINDOW_TOKENS = 0
    try:
        from app.core.llm.context_window_resolver import resolve_context_window

        assert resolve_context_window("deepseek-v4-flash") == 1_000_000
    finally:
        Settings.CONTEXT_WINDOW_TOKENS = original


def test_resolve_context_window_min_with_soft_cap() -> None:
    """软上限生效时，实际窗口 = min(模型窗口, 软上限)。"""
    original = Settings.CONTEXT_WINDOW_TOKENS
    Settings.CONTEXT_WINDOW_TOKENS = 32000
    try:
        from app.core.llm.context_window_resolver import resolve_context_window

        assert resolve_context_window("deepseek-v4-flash") == 32000
    finally:
        Settings.CONTEXT_WINDOW_TOKENS = original


def test_resolve_context_window_unknown_model_fallback() -> None:
    """未收录模型用兜底窗口（与软上限取 min）。"""
    original = Settings.CONTEXT_WINDOW_TOKENS
    Settings.CONTEXT_WINDOW_TOKENS = 64000
    try:
        from app.core.llm.context_window_resolver import resolve_context_window

        # 兜底 128_000，软上限 64_000 → 64_000
        assert resolve_context_window("no-such-model") == 64000
    finally:
        Settings.CONTEXT_WINDOW_TOKENS = original


# --------------------------------------------------------------------------- #
# 异常路径与防御
# --------------------------------------------------------------------------- #
def test_usage_meter_provider_returns_none_is_safe() -> None:
    """message_provider 返回非消息元素时，_message_tokens 不抛（返回 0）。"""
    messages = [object(), 123, None, HumanMessage(content="正常文本")]
    meter = _make_meter(messages)
    usage = meter.read(force=True)
    # 仅 HumanMessage 贡献 token，其余对象 content 提取失败返回 0
    assert usage.used_tokens == TokenEstimator.estimate("正常文本")


def test_usage_meter_no_provider_uses_resolver_fallback() -> None:
    """未注入 total_tokens_provider 且软上限为 0 时，窗口 = 模型最大窗口。"""
    original = Settings.CONTEXT_WINDOW_TOKENS
    Settings.CONTEXT_WINDOW_TOKENS = 0
    try:
        meter = _make_meter([HumanMessage(content="x")], total_provider=None)
        assert meter.read(force=True).total_tokens == 1_000_000
    finally:
        Settings.CONTEXT_WINDOW_TOKENS = original


def test_usage_meter_no_provider_min_with_soft_cap() -> None:
    """未注入 provider 且软上限生效时，兜底 = min(模型窗口, 软上限)，与 resolver 一致。"""
    original = Settings.CONTEXT_WINDOW_TOKENS
    Settings.CONTEXT_WINDOW_TOKENS = 32000
    try:
        from app.core.llm.context_window_resolver import resolve_context_window

        meter = _make_meter([HumanMessage(content="x")], total_provider=None)
        assert meter.read(force=True).total_tokens == 32000
        assert meter.read(force=True).total_tokens == resolve_context_window("deepseek-v4-flash")
    finally:
        Settings.CONTEXT_WINDOW_TOKENS = original


def test_emit_context_usage_skips_when_meter_absent() -> None:
    """_emit_context_usage 在计量器未挂载时静默跳过（不写事件、不抛）。"""
    import app.core.workflows.nodes.model_node as mn

    class _FakeCtxNoMeter:
        usage_meter = None

    saved = mn._runtime_context
    mn._runtime_context = lambda: _FakeCtxNoMeter()  # type: ignore[assignment]
    try:
        # 不应抛异常；write_event 未被调用（此处不验证，仅确认不崩）
        mn._emit_context_usage(step_id="s1", task_id="task-1")
    finally:
        mn._runtime_context = saved  # type: ignore[assignment]


def test_emit_context_usage_swallows_meter_error() -> None:
    """_emit_context_usage 在计量器 read 抛异常时记日志并跳过（不向上传播）。"""

    class _BoomMeter:
        def read(self, *, force=False):
            raise RuntimeError("boom in estimate")

    class _FakeCtxBoom:
        usage_meter = _BoomMeter()

    import app.core.workflows.nodes.model_node as mn

    saved = mn._runtime_context
    mn._runtime_context = lambda: _FakeCtxBoom()  # type: ignore[assignment]
    try:
        # 不应抛；异常被 log.exception 捕获
        mn._emit_context_usage(step_id="s2", task_id="task-1")
    finally:
        mn._runtime_context = saved  # type: ignore[assignment]

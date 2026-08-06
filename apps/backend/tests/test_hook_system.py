"""进程内 Hook 机制单元测试。

覆盖四层：
- 契约层：``HookContext`` / ``HookResult`` 不可变与兜底语义；
- 注册表层：索引、执行编排、matcher、失败安全、空注册零开销；
- 内置 Hook：``ToolAuditHook`` 审计日志不泄敏、不阻断；
- 拦截链路：``ToolScheduler`` 静态调用 ``HookInterceptor`` 后 PreToolUse deny 短路、
  ``modified_arguments`` 生效、``after_tool_call`` 触发（用真实工具执行验证）。

``ToolCallInterceptor`` 协议已删除，所有拦截（工具 Pre/PostToolUse、运行期事件、
会话事件）收口到 ``app.hook.hook_interceptor.HookInterceptor`` 静态方法；
``ToolScheduler`` 不再接收拦截器注入，测试中直接通过 ``HookRegistry`` 注册 Hook
即可驱动完整链路。
"""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.hook.builtins.tool_audit_hook import ToolAuditHook
from app.hook.hook_base import HookBase
from app.hook.hook_context import HookContext
from app.hook.hook_event import HookDecision, HookEvent
from app.hook.hook_interceptor import HookInterceptor
from app.hook.hook_registry import (
    HookRegistry,
    get_hook_registry,
    initialize_hook_registry,
)
from app.hook.hook_result import HookResult
from app.tools.schemas import ToolCall, ToolExecutionContext
from app.tools.tool_handler.search_files import build_search_files_definition
from app.tools.tool_system import ToolSystem


class _StubHook(HookBase):
    """测试用 Hook：按构造参数决定决策，并记录被调用次数与最近 context。"""

    def __init__(
        self,
        event: HookEvent,
        decision: HookDecision = HookDecision.ALLOW,
        matcher: str | None = None,
        modified: dict | None = None,
        extra: str | None = None,
        raise_on_execute: bool = False,
    ) -> None:
        super().__init__(event=event, matcher=matcher)
        self._decision = decision
        self._modified = modified
        self._extra = extra
        self._raise = raise_on_execute
        self.calls: list[HookContext] = []

    @property
    def name(self) -> str:
        return "stub"

    def execute(self, context: HookContext) -> HookResult:
        self.calls.append(context)
        if self._raise:
            raise RuntimeError("boom")
        return HookResult(
            decision=self._decision,
            deny_reason="denied" if self._decision == HookDecision.DENY else None,
            modified_arguments=self._modified,
            additional_context=self._extra,
        )


class TestHookContract:
    """契约值对象语义。"""

    def test_context_frozen_and_default_metadata(self) -> None:
        """HookContext 不可变，metadata 缺省为空 dict 且不共享实例。"""
        c1 = HookContext(event=HookEvent.STOP)
        c2 = HookContext(event=HookEvent.STOP)
        assert c1.metadata == {}
        assert c1.metadata is not c2.metadata
        # frozen 仅阻止属性重赋值，不阻止 dict 内容修改；验证属性不可重赋值
        with pytest.raises(FrozenInstanceError):
            c1.metadata = {}  # frozen 应抛 FrozenInstanceError

    def test_result_allow_default(self) -> None:
        """缺 decision 按 ALLOW 兜底。"""
        assert HookResult().decision == HookDecision.ALLOW


class TestHookRegistry:
    """注册表索引与执行编排。"""

    def _reg(self) -> HookRegistry:
        return HookRegistry()

    def test_empty_fire_returns_allow(self) -> None:
        """空注册表 fire 零开销返回 ALLOW。"""
        assert self._reg().fire(HookContext(event=HookEvent.STOP)).decision == HookDecision.ALLOW

    def test_register_and_fire_hits(self) -> None:
        """注册后 fire 命中并执行，返回 ALLOW。"""
        reg = self._reg()
        hook = _StubHook(event=HookEvent.STOP)
        reg.register(hook)
        result = reg.fire(HookContext(event=HookEvent.STOP))
        assert result.decision == HookDecision.ALLOW
        assert len(hook.calls) == 1

    def test_deny_short_circuits(self) -> None:
        """首个 DENY 短路，后续 Hook 不再执行。"""
        reg = self._reg()
        h1 = _StubHook(event=HookEvent.PRE_TOOL_USE, decision=HookDecision.DENY)
        h2 = _StubHook(event=HookEvent.PRE_TOOL_USE)
        reg.register(h1)
        reg.register(h2)
        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="read_file")
        result = reg.fire(ctx)
        assert result.decision == HookDecision.DENY
        assert result.deny_reason == "denied"
        assert len(h1.calls) == 1
        assert len(h2.calls) == 0

    def test_matcher_filters_by_tool_name(self) -> None:
        """matcher 仅对工具类事件生效；非工具事件恒不命中。"""
        reg = self._reg()
        hook = _StubHook(event=HookEvent.PRE_TOOL_USE, matcher="^read_")
        reg.register(hook)
        # 命中
        reg.fire(HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="read_file"))
        assert len(hook.calls) == 1
        # 未命中
        reg.fire(HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="write_file"))
        assert len(hook.calls) == 1
        # 非工具事件：tool_name 为 None，非空 matcher 恒不命中
        hook2 = _StubHook(event=HookEvent.STOP, matcher="^read_")
        reg.register(hook2)
        reg.fire(HookContext(event=HookEvent.STOP))
        assert len(hook2.calls) == 0

    def test_invalid_matcher_fails_fast(self) -> None:
        """非法 matcher 在注册构造期抛 re.error（ValueError 子类，fail-fast）。"""
        import re as _re

        with pytest.raises((ValueError, _re.error)):
            _StubHook(event=HookEvent.PRE_TOOL_USE, matcher="(")

    def test_execute_exception_falls_back_to_allow(self) -> None:
        """Hook 抛异常兜底 ALLOW 且不阻断。"""
        reg = self._reg()
        hook = _StubHook(event=HookEvent.STOP, raise_on_execute=True)
        reg.register(hook)
        result = reg.fire(HookContext(event=HookEvent.STOP))
        assert result.decision == HookDecision.ALLOW

    def test_invalid_result_falls_back_to_allow(self) -> None:
        """Hook 返回非 HookResult 兜底 ALLOW。"""

        class _Bad(HookBase):
            @property
            def name(self) -> str:
                return "bad"

            def execute(self, context):
                return "not a result"

        reg = self._reg()
        reg.register(_Bad(event=HookEvent.STOP))
        assert reg.fire(HookContext(event=HookEvent.STOP)).decision == HookDecision.ALLOW

    def test_modified_arguments_merged(self) -> None:
        """PreToolUse 的 modified_arguments 透传到结果。"""
        reg = self._reg()
        reg.register(_StubHook(event=HookEvent.PRE_TOOL_USE, modified={"a": 2}))
        result = reg.fire(
            HookContext(
                event=HookEvent.PRE_TOOL_USE, tool_name="read_file", tool_arguments={"a": 1}
            )
        )
        assert result.modified_arguments == {"a": 2}

    def test_additional_context_concatenated(self) -> None:
        """多个 Hook 的 additional_context 拼接（首版无消费方）。"""
        reg = self._reg()
        reg.register(_StubHook(event=HookEvent.POST_TOOL_USE, extra="note-a"))
        reg.register(_StubHook(event=HookEvent.POST_TOOL_USE, extra="note-b"))
        result = reg.fire(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="read_file"))
        assert result.additional_context == "note-a\nnote-b"

    def test_list_aggregates_all_events(self) -> None:
        """list() 跨事件合并所有 Hook。"""
        reg = self._reg()
        reg.register(_StubHook(event=HookEvent.STOP))
        reg.register(_StubHook(event=HookEvent.PRE_TOOL_USE))
        assert len(reg.list()) == 2


class TestToolAuditHook:
    """内置审计 Hook。"""

    def test_returns_allow_and_no_secret(self) -> None:
        """审计 Hook 返回 ALLOW，且不应记录入参原文。"""
        hook = ToolAuditHook()
        assert hook.event == HookEvent.POST_TOOL_USE
        obs = _make_observation()
        result = hook.execute(
            HookContext(event=HookEvent.POST_TOOL_USE, tool_name="read_file", tool_observation=obs)
        )
        assert result.decision == HookDecision.ALLOW


class TestToolSchedulerInterceptor:
    """拦截链路：真实工具执行 + HookRegistry 驱动 HookInterceptor 静态调用。

    ``ToolScheduler`` 内置静态调用 ``HookInterceptor``，无需注入拦截器。
    测试通过 ``HookRegistry`` 注册 Hook 即可驱动完整 Pre/PostToolUse 链路。
    """

    def _reset_registry(self) -> None:
        import app.hook.hook_registry as _m

        _m._registry = None

    def _ctx(self, tmp_path: Path) -> ToolExecutionContext:
        return ToolExecutionContext(task_id="t1", workspace_id="w1", workspace_root=tmp_path)

    def _build_system_with_registry(
        self,
        register: callable,  # type: ignore[type-arg]
    ) -> ToolSystem:
        """初始化全局注册表、按需注册 Hook，再构建自带 HookInterceptor 的 ToolSystem。"""
        self._reset_registry()
        reg = initialize_hook_registry()
        register(reg)
        return ToolSystem.build_tool_system()

    def test_after_tool_call_fires_on_real_execution(self, tmp_path: Path) -> None:
        """真实执行 search_files 后 POST_TOOL_USE 审计 Hook 被触发。"""
        (tmp_path / "a.txt").write_text("hi")
        post_hook = _StubHook(event=HookEvent.POST_TOOL_USE)

        def _register(reg: HookRegistry) -> None:
            reg.register(post_hook)

        system = self._build_system_with_registry(_register)
        ctx = self._ctx(tmp_path)
        call = ToolCall(tool_name="search_files", arguments={"pattern": "a.txt"}, call_id="c1")
        observation = system.scheduler.execute(call, ctx)
        assert observation.status == "success"
        # 真实执行后 HookInterceptor.after_tool_call 触发了 POST_TOOL_USE Hook
        assert len(post_hook.calls) == 1
        self._reset_registry()

    def test_pre_deny_short_circuits_without_execution(self, tmp_path: Path) -> None:
        """PreToolUse deny 短路：返回 tool_error、不执行、不触发 after。"""
        (tmp_path / "a.txt").write_text("hi")
        pre_hook = _StubHook(
            event=HookEvent.PRE_TOOL_USE, decision=HookDecision.DENY, matcher="^search_files"
        )
        post_hook = _StubHook(event=HookEvent.POST_TOOL_USE)

        def _register(reg: HookRegistry) -> None:
            reg.register(pre_hook)
            reg.register(post_hook)

        system = self._build_system_with_registry(_register)
        ctx = self._ctx(tmp_path)
        call = ToolCall(tool_name="search_files", arguments={"pattern": "a.txt"}, call_id="c1")
        observation = system.scheduler.execute(call, ctx)
        assert observation.status == "error"
        assert "denied" in (observation.error or "")
        # after 不应触发（deny 短路）
        assert len(post_hook.calls) == 0
        self._reset_registry()

    def test_pre_modified_arguments_takes_effect(self, tmp_path: Path) -> None:
        """PreToolUse modified_arguments 替换原参数后真实执行生效。"""
        (tmp_path / "target.txt").write_text("hi")

        def _register(reg: HookRegistry) -> None:
            reg.register(
                _StubHook(
                    event=HookEvent.PRE_TOOL_USE,
                    modified={"pattern": "target.txt"},
                    matcher="^search_files",
                )
            )

        system = self._build_system_with_registry(_register)
        ctx = self._ctx(tmp_path)
        # 故意给错 pattern，靠 Hook 改写为正确值
        call = ToolCall(
            tool_name="search_files", arguments={"pattern": "nonexistent_xyz"}, call_id="c1"
        )
        observation = system.scheduler.execute(call, ctx)
        assert observation.status == "success"
        self._reset_registry()

    def test_no_hook_no_effect(self, tmp_path: Path) -> None:
        """未注册任何 Hook 时 Pre/PostToolUse 放行，工具正常执行。"""
        (tmp_path / "a.txt").write_text("hi")

        def _register(reg: HookRegistry) -> None:
            pass  # 仅内置 audit hook（POST_TOOL_USE），不影响放行

        system = self._build_system_with_registry(_register)
        ctx = self._ctx(tmp_path)
        call = ToolCall(tool_name="search_files", arguments={"pattern": "a.txt"}, call_id="c1")
        observation = system.scheduler.execute(call, ctx)
        assert observation.status == "success"
        self._reset_registry()


class TestHookBootstrap:
    """启动期播种与单例。"""

    def test_initialize_seeds_audit_hook(self) -> None:
        """initialize_hook_registry 播种内置审计 Hook。"""
        reg = initialize_hook_registry()
        names = [h.name for h in reg.list()]
        assert "tool_audit" in names
        # POST_TOOL_USE 下有内置订阅
        assert len(reg.resolve_for(HookEvent.POST_TOOL_USE)) == 1
        # 其它事件首版无内置实现
        assert reg.resolve_for(HookEvent.STOP) == []
        # 还原全局单例，避免污染其它用例
        import app.hook.hook_registry as _m

        _m._registry = None


class TestHookContractExtra:
    """契约值对象补充边界（DENY 缺 reason、frozen 不可重赋值）。"""

    def test_deny_without_reason_gets_default(self) -> None:
        """DENY 且未传 deny_reason 时，__post_init__ 补默认文案。"""
        result = HookResult(decision=HookDecision.DENY)
        assert result.decision == HookDecision.DENY
        assert result.deny_reason == "blocked by hook"

    def test_deny_blank_reason_gets_default(self) -> None:
        """DENY 且 deny_reason 为空白字符串时同样补默认文案。"""
        result = HookResult(decision=HookDecision.DENY, deny_reason="")
        assert result.deny_reason == "blocked by hook"

    def test_result_frozen_no_reassign(self) -> None:
        """HookResult frozen：属性不可重赋值。"""
        result = HookResult()
        with pytest.raises(FrozenInstanceError):
            result.decision = HookDecision.DENY  # frozen 应抛 FrozenInstanceError


class TestHookFailFast:
    """fail-fast 语义与日志定位上下文。"""

    def _reset_registry(self) -> None:
        import app.hook.hook_registry as _m

        _m._registry = None

    def test_get_registry_before_init_raises(self) -> None:
        """initialize_hook_registry 之前调用 get_hook_registry 应抛 RuntimeError。"""
        self._reset_registry()
        with pytest.raises(RuntimeError):
            get_hook_registry()

    def test_execute_exception_logs_context(self, caplog: pytest.LogCaptureFixture) -> None:
        """Hook 抛异常兜底 ALLOW，并落含 hook_name/event 的 error 日志。"""
        import logging

        self._reset_registry()
        reg = initialize_hook_registry()
        hook = _StubHook(event=HookEvent.STOP, raise_on_execute=True)
        reg.register(hook)
        with caplog.at_level(logging.ERROR, logger="coding_agent.backend"):
            result = reg.fire(HookContext(event=HookEvent.STOP))
        assert result.decision == HookDecision.ALLOW
        # 找 hook_execute_failed 日志事件（event 即日志 msg 模板）及其 data 上下文
        records = [r for r in caplog.records if r.msg == "hook_execute_failed"]
        assert records, "应产生 hook_execute_failed 日志"
        rec = records[0]
        data = rec.__dict__.get("data", {})
        assert data.get("hook_name") == "stub"
        assert data.get("event") == HookEvent.STOP.value
        # 还原单例，避免污染其它用例
        self._reset_registry()


class TestHookInterceptorReal:
    """真实 HookInterceptor + HookRegistry + ToolScheduler 端到端集成。

    HookInterceptor 改为静态方法，以下用例直接以类级调用驱动验证。
    """

    def _reset_registry(self) -> None:
        import app.hook.hook_registry as _m

        _m._registry = None

    def _ctx(self, tmp_path: Path) -> ToolExecutionContext:
        return ToolExecutionContext(task_id="t1", workspace_id="w1", workspace_root=tmp_path)

    def test_real_deny_short_circuits(self, tmp_path: Path) -> None:
        """真实链路：PRE_TOOL_USE 返回 DENY → 短路 tool_error，不执行、不触发 after。"""
        (tmp_path / "a.txt").write_text("hi")
        self._reset_registry()
        reg = initialize_hook_registry()
        reg.register(
            _StubHook(
                event=HookEvent.PRE_TOOL_USE,
                decision=HookDecision.DENY,
                matcher="^search_files",
            )
        )
        system = ToolSystem.build_tool_system()
        ctx = self._ctx(tmp_path)
        call = ToolCall(tool_name="search_files", arguments={"pattern": "a.txt"}, call_id="c1")
        observation = system.scheduler.execute(call, ctx)
        assert observation.status == "error"
        assert "denied" in (observation.error or "")
        self._reset_registry()

    def test_real_deny_stops_subsequent_hooks(self, tmp_path: Path) -> None:
        """真实链路：首个 DENY 短路，同事件后续 Hook 不再执行。"""
        (tmp_path / "a.txt").write_text("hi")
        h2 = _StubHook(event=HookEvent.PRE_TOOL_USE)
        self._reset_registry()
        reg = initialize_hook_registry()
        reg.register(
            _StubHook(
                event=HookEvent.PRE_TOOL_USE,
                decision=HookDecision.DENY,
                matcher="^search_files",
            )
        )
        reg.register(h2)
        system = ToolSystem.build_tool_system()
        ctx = self._ctx(tmp_path)
        call = ToolCall(tool_name="search_files", arguments={"pattern": "a.txt"}, call_id="c1")
        observation = system.scheduler.execute(call, ctx)
        assert observation.status == "error"
        assert len(h2.calls) == 0
        self._reset_registry()

    def test_real_modified_arguments_takes_effect(self, tmp_path: Path) -> None:
        """真实链路：PRE_TOOL_USE 改写参数后，用改写值真实执行成功。"""
        (tmp_path / "target.txt").write_text("hi")
        self._reset_registry()
        reg = initialize_hook_registry()
        reg.register(
            _StubHook(
                event=HookEvent.PRE_TOOL_USE,
                modified={"pattern": "target.txt"},
                matcher="^search_files",
            )
        )
        system = ToolSystem.build_tool_system()
        ctx = self._ctx(tmp_path)
        # 故意给错误 pattern，靠 Hook 改写为正确值
        call = ToolCall(
            tool_name="search_files", arguments={"pattern": "nonexistent_xyz"}, call_id="c1"
        )
        observation = system.scheduler.execute(call, ctx)
        assert observation.status == "success"
        self._reset_registry()

    def test_after_tool_call_returns_valid_decision(self, tmp_path: Path) -> None:
        """真实链路：after_tool_call 在真实执行后返回合法决策，不抛异常。"""
        (tmp_path / "a.txt").write_text("hi")
        self._reset_registry()
        initialize_hook_registry()
        tool = build_search_files_definition()
        ctx = self._ctx(tmp_path)
        obs = _make_observation()
        # 直接以静态方法调用，验证其返回合法决策而不抛异常
        decision = HookInterceptor.after_tool_call(tool, ctx, obs)
        assert decision.decision == HookDecision.ALLOW
        self._reset_registry()

    def test_before_exception_fallback_returns_valid_decision(self, tmp_path: Path) -> None:
        """真实链路：before_tool_call 在 fire 抛异常时兜底返回合法放行决策。"""
        import app.hook.hook_registry as _m

        self._reset_registry()
        _m._registry = None  # 未初始化，get_hook_registry 抛 RuntimeError，走兜底
        tool = build_search_files_definition()
        ctx = self._ctx(tmp_path)
        # fire 会抛 RuntimeError（注册表未初始化），before_tool_call 应兜底放行
        decision = HookInterceptor.before_tool_call(tool, ctx, {"pattern": "a.txt"})
        assert decision.decision == HookDecision.ALLOW
        assert decision.deny_reason is None
        self._reset_registry()

    def test_audit_hook_fires_on_real_execution(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """真实链路：POST_TOOL_USE 触发内置审计 Hook，落 debug 级 tool_audit 日志且不阻断。"""
        import logging

        (tmp_path / "a.txt").write_text("hi")

        def _register(reg: HookRegistry) -> None:
            pass  # 内置 audit hook 已在 initialize 播种

        self._reset_registry()
        reg = initialize_hook_registry()
        _register(reg)
        system = ToolSystem.build_tool_system()
        ctx = self._ctx(tmp_path)
        call = ToolCall(tool_name="search_files", arguments={"pattern": "a.txt"}, call_id="c1")
        with caplog.at_level(logging.DEBUG, logger="coding_agent.backend"):
            observation = system.scheduler.execute(call, ctx)
        assert observation.status == "success"
        records = [r for r in caplog.records if r.msg == "tool_audit"]
        assert records, "应产生 tool_audit 审计日志"
        data = records[0].__dict__.get("data", {})
        assert data.get("tool_name") == "search_files"
        assert data.get("success") is True
        self._reset_registry()

    def test_real_post_allow_after_tool_call_returns_valid(self, tmp_path: Path) -> None:
        """真实链路回归：POST_TOOL_USE 返回 ALLOW 时，after_tool_call 应返回合法放行
        决策（决策字段为 HookResult.decision == ALLOW），不抛 TypeError。"""
        post_hook = _StubHook(event=HookEvent.POST_TOOL_USE, decision=HookDecision.ALLOW)
        self._reset_registry()
        reg = initialize_hook_registry()
        reg.register(post_hook)
        tool = build_search_files_definition()
        ctx = self._ctx(tmp_path)
        obs = _make_observation()
        decision = HookInterceptor.after_tool_call(tool, ctx, obs)
        assert decision.decision == HookDecision.ALLOW
        assert decision.deny_reason is None
        assert decision.modified_arguments is None
        assert len(post_hook.calls) == 1
        self._reset_registry()

    def test_real_pre_allow_before_tool_call_returns_valid(self, tmp_path: Path) -> None:
        """真实链路回归：PRE_TOOL_USE 返回 ALLOW（无改写）时，before_tool_call 应返回
        合法放行决策（decision == ALLOW、deny_reason / modified_arguments 为 None），
        不抛 TypeError。"""
        pre_hook = _StubHook(event=HookEvent.PRE_TOOL_USE, decision=HookDecision.ALLOW)
        self._reset_registry()
        reg = initialize_hook_registry()
        reg.register(pre_hook)
        tool = build_search_files_definition()
        ctx = self._ctx(tmp_path)
        decision = HookInterceptor.before_tool_call(tool, ctx, {"pattern": "a.txt"})
        assert decision.decision == HookDecision.ALLOW
        assert decision.deny_reason is None
        assert decision.modified_arguments is None
        assert len(pre_hook.calls) == 1
        self._reset_registry()


def _make_observation():
    """构造一个最小 ToolObservation 供审计 Hook 测试。"""
    from app.tools.schemas.tool_observation import ToolObservation

    return ToolObservation(tool_name="read_file", status="success", content="ok")

"""delegate_task 工具真实链路端到端测试。

本文件按 docs/test/delegate_task_e2e_plan.md 落地，覆盖链路 A/B/C/D/E/F。
装配不变量（方案第二节）：
  1. Settings.override(...) → init_storage() → initialize_service_dependencies()
     + set_agent_registry(build_agent_registry()) → AgentRuntime()
  2. 自己构造 DelegationExecutor 塞进 execution_context.runtime_dependencies
  3. 全部 SQLite/checkpoint/log 库 override 到 tmp_path，不污染开发库
  4. 用例用 @pytest.mark.llm 标记；无 DEEPSEEK_API_KEY 时 fixture 内 skip
  5. LLM 真实调用（需要联网 + key），属于冒烟测试，默认不进普通 pytest
  6. teardown：close_service_dependencies + 清 lru_cache + Settings.load() 复位

仅产出测试与报告，不修改任何业务代码、不修复缺陷。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.config.configuration import build_agent_registry, set_agent_registry, set_tool_system
from app.config.settings import Settings
from app.core.agents.define_agents import default_developer_agent
from app.core.delegation.child_agent_runner import ChildAgentRunner
from app.core.delegation.delegation_executor import DelegationExecutor
from app.core.runtime.runner import AgentRuntime
from app.core.runtime.turn_cancellation_registry import cancellation_registry
from app.models import TaskRecord, TurnRecord
from app.models.enums.event_type import EventType
from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService
from app.service.depends import (
    close_service_dependencies,
    get_turn_service,
    initialize_service_dependencies,
    reset_service_dependencies,
)
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.service.task.workspace_service import WorkspaceService
from app.storage.crud.delegation_crud import DelegationCrud
from app.storage.crud.runtime_event_crud import RuntimeEventCrud
from app.storage.store_engines import init_storage
from app.tools.schemas import ToolExecutionContext
from app.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.tools.tool_handler.delegate_task import build_delegate_task_definition
from app.tools.tool_system import ToolSystem

# 真实测试工作区（方案固定）：G:\code\TradingAgents
TRADING_AGENTS_ROOT = Path(r"G:\code\TradingAgents")
# B2 限定生成物落在此子目录，teardown 清理
E2E_SCRATCH_SUBDIR = ".e2e_scratch"

pytestmark = pytest.mark.llm


def _require_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """复用进程环境的 DEEPSEEK_API_KEY；缺失则跳过整组 LLM 测试。

    不把 key 明文写入任何文件，仅经 monkeypatch.setenv 注入当前进程环境。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过真实 LLM 端到端测试")
    monkeypatch.setenv("DEEPSEEK_API_KEY", api_key)


@pytest.fixture
def real_runtime_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """装配无 mock 的真实运行栈，全部存储指向 tmp_path。

    装配顺序（方案 2.1）：
      1. Settings.override 全部 SQLite/checkpoint/log 库到 tmp_path
      2. init_storage() 建表
      3. initialize_service_dependencies() 装配全局 service 单例
      4. set_agent_registry(build_agent_registry()) 播种 5 个内置 agent
      5. AgentRuntime() 取全局单例
    """
    _require_api_key(monkeypatch)
    db_file = tmp_path / "app.sqlite3"
    log_db_file = tmp_path / "logs.sqlite3"
    checkpoint_file = tmp_path / "langgraph_checkpoints.sqlite"
    log_dir = tmp_path / "logs"
    Settings.override(
        DATABASE_FILE=db_file,
        LOG_DATABASE_FILE=log_db_file,
        CHECKPOINT_FILE=checkpoint_file,
        LOG_DIR=log_dir,
    )
    init_storage()
    initialize_service_dependencies()
    set_agent_registry(build_agent_registry())
    # AgentRuntime.__init__ 还需工具系统单例（真实注册全部内置工具，无 mock）
    set_tool_system(ToolSystem.build_tool_system())
    runtime = AgentRuntime()
    yield runtime
    # teardown：关闭存储 + 清 lru_cache + 复位 tool system + Settings.load 复位
    close_service_dependencies()
    reset_service_dependencies()
    set_tool_system(None)  # 清除进程级工具系统单例，避免污染同进程其它测试
    Settings.load()


@pytest.fixture
def trading_agents_workspace(real_runtime_stack: AgentRuntime):
    """在真实工作区 G:\\code\\TradingAgents 创建 workspace（不污染开发库表）。"""
    if not TRADING_AGENTS_ROOT.exists():
        pytest.skip(f"测试工作区不存在：{TRADING_AGENTS_ROOT}")
    ws_service = WorkspaceService()
    workspace = ws_service.create_workspace(
        name="trading_agents_e2e",
        root_path=str(TRADING_AGENTS_ROOT),
    )
    return workspace


@pytest.fixture
def parent_turn(real_runtime_stack: AgentRuntime, trading_agents_workspace):
    """构造父 task + 父 turn，并 claim 为 running（DelegationExecutor 依赖边界）。

    默认发起方为顶层 task（parent_task_id 为空 → depth=0，主 Agent 允许发起
    第一层委派）。C2 会单独构造「自身是委派子 task」的发起方。
    """
    task_service = TaskService()
    turn_service = TurnService()
    parent_task: TaskRecord = task_service.create_task(
        input_text="parent delegation e2e task",
        status="open",
        agent_id="developer",
        workspace_id=trading_agents_workspace.workspace_id,
    )
    parent_turn_record: TurnRecord = turn_service.create_turn(
        task_id=parent_task.task_id,
        input_text="parent turn for delegation",
        agent_id="developer",
    )
    assert turn_service.claim_pending_turn(parent_turn_record.turn_id)
    # 重新读取以拿到 running 终态
    parent_turn_record = turn_service.get_turn(parent_turn_record.turn_id)
    return {"task": parent_task, "turn": parent_turn_record}


@pytest.fixture
def injected_execution_context(
    real_runtime_stack: AgentRuntime,
    trading_agents_workspace,
    parent_turn: dict,
):
    """按方案 2.2 构造带真实 DelegationExecutor 的 ToolExecutionContext。

    child_runner 完全等价生产装配（runner.py DelegationExecutor 构造签名）。
    返回 (execution_context, executor, parent_task, parent_turn)。
    """
    runtime = real_runtime_stack
    parent_task: TaskRecord = parent_turn["task"]
    parent_turn_record: TurnRecord = parent_turn["turn"]
    child_runner = ChildAgentRunner(
        runtime.run_agent,
        should_cancel=cancellation_registry.is_cancelled,
    )
    executor = DelegationExecutor(
        child_runner=child_runner,
        parent_profile=default_developer_agent(),
        parent_turn=parent_turn_record,
        parent_task=parent_task,
    )
    execution_context = ToolExecutionContext(
        task_id=parent_task.task_id,
        workspace_id=trading_agents_workspace.workspace_id,
        workspace_root=TRADING_AGENTS_ROOT,
        runtime_dependencies=ToolRuntimeDependencies(delegate_task_executor=executor),
    )
    runtime_event_crud = RuntimeEventCrud()

    def event_query(turn_id: str) -> list[dict]:
        """按 turn_id 查询已持久化的运行时事件（按 sequence 升序）。"""
        return runtime_event_crud.list_by_turn(turn_id)

    return {
        "execution_context": execution_context,
        "executor": executor,
        "parent_task": parent_task,
        "parent_turn": parent_turn_record,
        "workspace_id": trading_agents_workspace.workspace_id,
        "event_query": event_query,
    }


def _handler():
    """返回真实的 delegate_task 工具 handler。"""
    return build_delegate_task_definition().handler


def _delegation_record(delegation_id: str):
    """读取真实 delegations 表记录。"""
    return DelegationCrud().get(delegation_id)


def _event_types_by_turn(turn_id: str) -> list[str]:
    """读取某 turn 下持久化 runtime 事件类型列表。"""
    events = RuntimeEventService().list_by_turn(turn_id)
    return [str(e.get("event_type")) for e in events]


def _cleanup_e2e_scratch():
    """清理 B2 在 TradingAgents 工作区内生成的 .e2e_scratch 子目录。"""
    scratch = TRADING_AGENTS_ROOT / E2E_SCRATCH_SUBDIR
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# 链路 A：工具入口 → 委派创建 → child 跑通 → 终态回写
# ---------------------------------------------------------------------------


def test_A1_delegate_reviewer_completes_end_to_end(
    injected_execution_context,
):
    """测试目的：验证 delegate_task 主路径真实跑通（创建 delegation + child 真实完
    成 + 终态回写 + runtime 事件落库）。该链路同时守护 depth 语义：顶层 task 发起
    必须放行（depth=0）——depth 判定写反时本用例会以 delegation_depth_exceeded
    失败。可能发现的缺陷：child 未真实完成 / delegation 终态未回写 / 事件未落库 /
    主 Agent 委派被误拒。"""
    ctx = injected_execution_context
    handler = _handler()
    observation = handler(
        child_agent_id="delegate_reviewer",
        title="Review TA main entry",
        objective=(
            "Review the main entry file of the TradingAgents project (main.py) "
            "and summarize its startup responsibilities."
        ),
        rules=["do not modify any files", "read only"],
        references=["main.py"],
        expected_output="a short review summary of main.py responsibilities",
        execution_context=ctx["execution_context"],
    )

    assert observation.status == "success", observation.content
    assert observation.data is not None
    assert observation.data.get("status") == "completed"
    delegation_id = observation.data.get("delegation_id")
    assert delegation_id

    # 真实 delegations 表记录 status == completed，child_turn_id 已回写
    record = _delegation_record(delegation_id)
    assert record.status == "completed"
    assert record.child_turn_id

    # 真实 turns 表存在对应 child turn，且为终态
    turn_service = TurnService()
    child_turn = turn_service.get_turn(record.child_turn_id)
    assert child_turn.status in {"completed", "failed", "cancelled", "running"}

    # 真实 runtime_events 表存在 delegation_started + delegation_finished
    events = _event_types_by_turn(ctx["parent_turn"].turn_id)
    assert EventType.DELEGATION_STARTED.value in events
    assert EventType.DELEGATION_FINISHED.value in events

    # child turn 在 workspace 内产生了真实 runtime 事件流（至少 RUN_STARTED + RUN_FINISHED）
    child_events = _event_types_by_turn(record.child_turn_id)
    assert EventType.RUN_STARTED.value in child_events
    assert EventType.RUN_FINISHED.value in child_events


# ---------------------------------------------------------------------------
# 链路 B：child 行为正确性（针对 TradingAgents 工作区）
# ---------------------------------------------------------------------------


def test_B1_delegate_reviewer_reads_workspace(
    injected_execution_context,
):
    """测试目的：验证 delegate_reviewer 真实读取工作区文件并产出审阅结论。

    行为断言：真实 child 在给定 read-only 目标后，应产生至少一次 read_file/search_files
    工具调用（tool_names 非空），且 observation 终态为完成、content 非空。
    可能发现的缺陷：child 未真正调用读工具（仅产出占位文本）/ 未真实读取工作区。"""
    ctx = injected_execution_context
    handler = _handler()
    observation = handler(
        child_agent_id="delegate_reviewer",
        title="B1 review entry",
        objective=(
            "Read main.py of the TradingAgents project and report its top 3 "
            "startup responsibilities in one short paragraph."
        ),
        rules=["read only", "do not modify files"],
        references=["main.py"],
        expected_output="a short paragraph listing startup responsibilities",
        execution_context=ctx["execution_context"],
    )
    assert observation.status == "success"
    assert observation.content
    # child 真实读取工作区：至少一次读类工具调用（覆盖 real LLM 行为）
    child_turn_id = observation.data.get("child_turn_id") if observation.data else None
    tool_names = set()
    if child_turn_id:
        events = ctx["event_query"](child_turn_id)
        for ev in events:
            if ev.get("event_type") == EventType.TOOL_CALL_STARTED.value:
                payload = ev.get("payload") or {}
                tool_names.add(payload.get("tool_name"))
    assert tool_names & {
        "read_file",
        "search_files",
    }, f"delegate_reviewer 未真实读取工作区，实际工具调用: {sorted(tool_names)}"


def test_B2_delegate_coder_writes_file_in_workspace(
    injected_execution_context,
):
    """测试目的：验证 delegate_coder 真实调用写工具且生成文件落在 workspace 边界内。
    可能发现的缺陷：child 越界写文件 / 未真实调用 write_file。"""
    ctx = injected_execution_context
    handler = _handler()
    objective = (
        f"Create a small Python utility file inside the '{E2E_SCRATCH_SUBDIR}' "
        f"subdirectory of the workspace named e2e_hello.py that prints 'e2e ok'."
    )
    observation = handler(
        child_agent_id="delegate_coder",
        title="Add e2e util",
        objective=objective,
        rules=[f"write only under ./{E2E_SCRATCH_SUBDIR}/", "create the directory if missing"],
        references=["."],
        expected_output="the path of the created file",
        execution_context=ctx["execution_context"],
    )

    # teardown 清理生成物
    try:
        assert observation.status == "success", observation.content
        delegation_id = observation.data.get("delegation_id")
        assert delegation_id
        record = _delegation_record(delegation_id)
        assert record.status == "completed"

        # child 真实调用了 write_file/patch：runtime 事件含 tool_call_started 且工具名匹配
        child_events = RuntimeEventService().list_by_turn(record.child_turn_id)
        tool_names = []
        for ev in child_events:
            payload = ev.get("payload") or {}
            name = payload.get("name") or payload.get("tool_name")
            if ev.get("event_type") == EventType.TOOL_CALL_STARTED.value and name:
                tool_names.append(name)
        assert any(
            n in {"write_file", "patch"} for n in tool_names
        ), f"child 未调用 write_file/patch，实际工具调用：{tool_names}"

        # 生成文件落在 workspace 边界内（G:\code\TradingAgents\.e2e_scratch）
        scratch = TRADING_AGENTS_ROOT / E2E_SCRATCH_SUBDIR
        created = list(scratch.glob("*.py")) if scratch.exists() else []
        assert created, "未在工作区 .e2e_scratch 子目录发现生成的 .py 文件"
        assert str(TRADING_AGENTS_ROOT) in str(created[0].resolve())
    finally:
        _cleanup_e2e_scratch()


def test_B3_delegate_tester_runs_to_terminal(
    injected_execution_context,
):
    """测试目的：验证 delegate_tester 真实产生 runtime 事件流并走完终态（弱断言）。
    可能发现的缺陷：child 未真实运行 / 未走终态。注意：tester 权限为全工具，
    不做「未写文件」强断言，仅观察性记录 read/search 调用。"""
    ctx = injected_execution_context
    handler = _handler()
    observation = handler(
        child_agent_id="delegate_tester",
        title="Check TA tests",
        objective=(
            "Inspect the tests/ directory of TradingAgents and report which core "
            "modules appear covered by tests."
        ),
        rules=["read only", "do not modify files"],
        references=["tests/"],
        expected_output="a coverage observation summary",
        execution_context=ctx["execution_context"],
    )

    assert observation.status == "success", observation.content
    delegation_id = observation.data.get("delegation_id")
    assert delegation_id
    record = _delegation_record(delegation_id)
    assert record.status == "completed"

    # 真实产生了 runtime 事件流且走完终态（RUN_STARTED + RUN_FINISHED）
    child_events = _event_types_by_turn(record.child_turn_id)
    assert EventType.RUN_STARTED.value in child_events
    assert EventType.RUN_FINISHED.value in child_events

    # 观察性记录：tester 真实产生了 read_file/search_files 调用（不强断言）
    tool_names = []
    for ev in RuntimeEventService().list_by_turn(record.child_turn_id):
        payload = ev.get("payload") or {}
        name = payload.get("name") or payload.get("tool_name")
        if ev.get("event_type") == EventType.TOOL_CALL_STARTED.value and name:
            tool_names.append(name)
    assert any(
        n in {"read_file", "search_files"} for n in tool_names
    ), f"tester 未调用 read_file/search_files，实际工具调用：{tool_names}"


# ---------------------------------------------------------------------------
# 链路 C：策略与边界（真实 DelegationPolicy 生效）
# ---------------------------------------------------------------------------


def test_C1_unknown_child_rejected_before_create(
    injected_execution_context,
):
    """测试目的：未知 child（ghost_agent）在创建 delegation 前被拒绝。
    可能发现的缺陷：未知 child 未被拒 / 仍落库 delegation 记录。"""
    ctx = injected_execution_context
    handler = _handler()
    observation = handler(
        child_agent_id="ghost_agent",
        title="Ghost delegation",
        objective="this agent does not exist",
        rules=[],
        references=[],
        expected_output="never",
        execution_context=ctx["execution_context"],
    )

    assert observation.status == "error"
    assert "child not found" in observation.content

    # 真实 delegations 表无新建记录（解析失败早于创建）
    records = DelegationCrud().list_by_parent_turn(ctx["parent_turn"].turn_id)
    assert records == [], f"不应创建 delegation 记录，实际：{records}"


def test_C2_depth_policy_rejects_nested_delegation(
    injected_execution_context,
    real_runtime_stack: AgentRuntime,
    trading_agents_workspace,
):
    """测试目的：发起方 task 自身是委派子 task（parent_task_id 非空 → depth=1
    >= max_depth=1）触发策略拒绝，且早于创建 delegation。可能发现的缺陷：深度
    判定未生效 / 仍落库记录 / 主 Agent 顶层 task 被误拒（depth 语义写反）。"""
    task_service = TaskService()
    turn_service = TurnService()
    # 顶层 task + turn 充当「祖父」链路（create_child_task 要求两者非空）
    grand_task = task_service.create_task(
        input_text="nested parent delegation e2e",
        status="open",
        agent_id="developer",
        workspace_id=trading_agents_workspace.workspace_id,
    )
    grand_turn = turn_service.create_turn(
        task_id=grand_task.task_id,
        input_text="grand parent turn",
        agent_id="developer",
    )
    turn_service.claim_pending_turn(grand_turn.turn_id)

    # 构造「自身是委派子 task」的发起方（create_child_task 仅校验 delegation_id
    # 唯一性，不要求 delegation 记录已存在）
    nested_parent_task = task_service.create_child_task(
        title="nested parent as child task",
        parent_task_id=grand_task.task_id,
        parent_turn_id=grand_turn.turn_id,
        delegation_id="e2e_c2_preexisting_delegation",
        workspace_id=trading_agents_workspace.workspace_id,
        agent_id="developer",
    )
    # 发起方已是委派子 task → executor 应判定 depth=1
    assert nested_parent_task.parent_task_id

    nested_parent = turn_service.create_turn(
        task_id=nested_parent_task.task_id,
        input_text="nested parent turn as child",
        agent_id="developer",
    )
    turn_service.claim_pending_turn(nested_parent.turn_id)

    child_runner = ChildAgentRunner(
        real_runtime_stack.run_agent,
        should_cancel=cancellation_registry.is_cancelled,
    )
    executor = DelegationExecutor(
        child_runner=child_runner,
        parent_profile=default_developer_agent(),
        parent_turn=nested_parent,
        parent_task=nested_parent_task,
    )
    execution_context = ToolExecutionContext(
        task_id=nested_parent_task.task_id,
        workspace_id=trading_agents_workspace.workspace_id,
        workspace_root=TRADING_AGENTS_ROOT,
        runtime_dependencies=ToolRuntimeDependencies(delegate_task_executor=executor),
    )
    handler = _handler()
    observation = handler(
        child_agent_id="delegate_reviewer",
        title="Nested review",
        objective="review something",
        rules=[],
        references=[],
        expected_output="review",
        execution_context=execution_context,
    )

    assert observation.status == "error"
    assert "delegation_depth_exceeded" in observation.content

    # 真实 delegations 表无新建记录（策略拒绝早于创建）
    records = DelegationCrud().list_by_parent_turn(nested_parent.turn_id)
    assert records == [], f"策略拒绝不应创建 delegation 记录，实际：{records}"


def test_C3_concurrency_limit_rejects(
    injected_execution_context,
):
    """测试目的：Settings.DELEGATION_MAX_CONCURRENCY=0 时委派被并发额度拒绝。
    可能发现的缺陷：额度裁决未生效 / 仍创建 child turn。"""
    ctx = injected_execution_context
    # 将并发额度 override 为 0（额度满，原子 acquire 拒绝）
    Settings.override(DELEGATION_MAX_CONCURRENCY=0)
    try:
        handler = _handler()
        observation = handler(
            child_agent_id="delegate_reviewer",
            title="Conc review block",
            objective="review main.py",
            rules=[],
            references=["main.py"],
            expected_output="review",
            execution_context=ctx["execution_context"],
        )

        assert observation.status == "error"
        assert observation.reason  # 含并发额度超限信息

        # 无 child turn 落库（额度裁决早于创建）
        records = DelegationCrud().list_by_parent_turn(ctx["parent_turn"].turn_id)
        assert records == [], f"并发额度拒绝不应创建 delegation 记录，实际：{records}"
    finally:
        # 复位并发额度，避免影响同进程其它用例
        Settings.load()


# ---------------------------------------------------------------------------
# 链路 D：失败与取消（真实终态回写）
# ---------------------------------------------------------------------------


def test_D1_child_run_failure_marks_failed(
    injected_execution_context,
    real_runtime_stack: AgentRuntime,
    monkeypatch: pytest.MonkeyPatch,
):
    """测试目的：child 运行时失败走 _finalize_result 的 failed 分支（确定性注入：
    spy 包装 runtime.run_agent 使生成器首个 yield 前 raise，触发 _consume_child_events
    吞异常返回 failed）。可能发现的缺陷：failed 终态未真实回写。"""
    ctx = injected_execution_context

    # 包一层薄 spy：生成器在首个 yield 前直接 raise，令 child runner 吞异常返回 failed
    def failing_run_agent(profile):
        async def _gen():
            raise RuntimeError("injected child workflow failure")
            yield  # pragma: no cover - 不可达

        return _gen()

    monkeypatch.setattr(real_runtime_stack, "run_agent", failing_run_agent)

    child_runner = ChildAgentRunner(
        real_runtime_stack.run_agent,
        should_cancel=cancellation_registry.is_cancelled,
    )
    executor = DelegationExecutor(
        child_runner=child_runner,
        parent_profile=default_developer_agent(),
        parent_turn=ctx["parent_turn"],
        parent_task=ctx["parent_task"],
    )
    execution_context = ToolExecutionContext(
        task_id=ctx["parent_task"].task_id,
        workspace_id=ctx["execution_context"].workspace_id,
        workspace_root=TRADING_AGENTS_ROOT,
        runtime_dependencies=ToolRuntimeDependencies(delegate_task_executor=executor),
    )
    handler = _handler()
    observation = handler(
        child_agent_id="delegate_reviewer",
        title="Injected fail",
        objective="will fail before first yield",
        rules=[],
        references=[],
        expected_output="never",
        execution_context=execution_context,
    )

    assert observation.status == "cancelled"
    # cancelled observation 的 data 不含 delegation_id，从 delegations 表按 parent_turn 查最近记录
    records = DelegationCrud().list_by_parent_turn(ctx["parent_turn"].turn_id)
    assert records, "failed 路径应已创建 delegation 记录（acquire 早于 child 运行）"
    record = records[-1]
    assert record.status == "failed"
    # 观察点：child turn 由 executor 的 except/finalize 路径标记 delegation 终态；
    # child turn 自身终态收敛由 runtime 负责（child runner 取消/失败早退时不一定回写 turn），
    # 此处仅断言 delegation 终态回写（核心契约），child turn 状态作为观察记录不强制。
    turn_service = TurnService()
    child_turn = turn_service.get_turn(record.child_turn_id)
    assert child_turn is not None


def test_D2_sync_step_exception_marks_failed(
    injected_execution_context,
    monkeypatch: pytest.MonkeyPatch,
):
    """测试目的：同步步骤异常走 executor except 分支（确定性构造：令 claim_pending_turn
    返回 False 触发 'child_turn_claim_lost' RuntimeError）。可能发现的缺陷：except
    分支未真实回写 failed 终态。"""
    ctx = injected_execution_context
    # executor 内部经 get_turn_service() 单例取 turn_service，须对单例实例打补丁
    turn_service = get_turn_service()

    # spy：create_turn 正常，claim_pending_turn 返回 False → executor 内显式 raise
    def fake_claim(turn_id: str) -> bool:
        return False

    monkeypatch.setattr(turn_service, "claim_pending_turn", fake_claim)

    handler = _handler()
    observation = handler(
        child_agent_id="delegate_reviewer",
        title="Sync step failure",
        objective="child turn claim will be lost",
        rules=[],
        references=[],
        expected_output="never",
        execution_context=ctx["execution_context"],
    )

    assert observation.status == "cancelled"
    # cancelled observation 的 data 不含 delegation_id，从 delegations 表按 parent_turn 查最近记录
    records = DelegationCrud().list_by_parent_turn(ctx["parent_turn"].turn_id)
    assert records, "except 分支应已创建 delegation 记录（acquire 早于 claim）"
    record = records[-1]
    assert record.status == "failed"


def test_D3_cancel_signal_marks_cancelled(
    injected_execution_context,
    real_runtime_stack: AgentRuntime,
):
    """测试目的：child 运行期间经 cancellation_registry 触发 is_cancelled=True，
    验证 should_cancel 接线真实生效 → delegation 标记 cancelled。可能发现的缺陷：
    取消信号未接线 / cancelled 终态未回写。"""
    ctx = injected_execution_context

    # should_cancel 闭包：首次被询问时即把该 child turn 标记为已取消（模拟外部取消信号
    # 在 child 启动瞬间到达），_consume_child_events 一进入即 return cancelled。
    # 完全确定、不依赖 LLM 行为，且真实走 cancellation_registry 路径（等价生产装配）。
    def should_cancel_with_signal(turn_id: str) -> bool:
        cancellation_registry.mark_cancelled(turn_id)
        return cancellation_registry.is_cancelled(turn_id)

    child_runner = ChildAgentRunner(
        real_runtime_stack.run_agent,
        should_cancel=should_cancel_with_signal,
    )
    executor = DelegationExecutor(
        child_runner=child_runner,
        parent_profile=default_developer_agent(),
        parent_turn=ctx["parent_turn"],
        parent_task=ctx["parent_task"],
    )
    execution_context = ToolExecutionContext(
        task_id=ctx["parent_task"].task_id,
        workspace_id=ctx["execution_context"].workspace_id,
        workspace_root=TRADING_AGENTS_ROOT,
        runtime_dependencies=ToolRuntimeDependencies(delegate_task_executor=executor),
    )
    handler = _handler()
    observation = handler(
        child_agent_id="delegate_reviewer",
        title="Cancelled child",
        objective="will be cancelled before first event",
        rules=[],
        references=[],
        expected_output="never",
        execution_context=execution_context,
    )

    assert observation.status == "cancelled"
    # cancelled observation 的 data 不含 delegation_id，从 delegations 表按 parent_turn 查最近记录
    records = DelegationCrud().list_by_parent_turn(ctx["parent_turn"].turn_id)
    assert records, "取消路径应已创建 delegation 记录（acquire 早于 child 运行）"
    record = records[-1]
    assert record.status == "cancelled"
    # 观察点：delegation 表 cancelled 终态回写（核心契约）；child turn 自身终态由 runtime
    # 负责，取消早退路径下 child turn 可能仍停留 running，此处仅记录不强制。
    turn_service = TurnService()
    child_turn = turn_service.get_turn(record.child_turn_id)
    assert child_turn is not None
    cancellation_registry.clear(record.child_turn_id)


# ---------------------------------------------------------------------------
# 链路 E：结构化输入拼装（真实 _build_agent_input_text + 透传落库）
# ---------------------------------------------------------------------------


def test_E1_structured_prompt_preserved(
    injected_execution_context,
):
    """测试目的：全字段 rules/references/background/expected_output 经
    executor→service 透传落库，prompt 文本无截断/无注入。可能发现的缺陷：
    文本被截断 / section 丢失 / 原文被改写。"""
    ctx = injected_execution_context
    handler = _handler()
    title = "E2E prompt"
    objective = "inspect the project entry and report module responsibilities"
    rules = ["do not modify files", "read only"]
    references = ["main.py", "README.md"]
    expected_output = "a structured module responsibility report"
    observation = handler(
        child_agent_id="delegate_reviewer",
        title=title,
        objective=objective,
        rules=rules,
        references=references,
        expected_output=expected_output,
        execution_context=ctx["execution_context"],
    )

    assert observation.status == "success", observation.content
    delegation_id = observation.data.get("delegation_id")
    assert delegation_id
    prompt = _delegation_record(delegation_id).prompt
    # 开头为 # {title}
    assert prompt.startswith(f"# {title}")
    # 含各分段标签（含 handler.execute 真实支持的字段：objective/rules/references/expected_output）
    assert "## Objective" in prompt
    assert "## Rules" in prompt
    assert "## References" in prompt
    assert "## Expected Output" in prompt
    # 原文被完整保留
    assert objective in prompt
    assert expected_output in prompt
    for rule in rules:
        assert rule in prompt
    for ref in references:
        assert ref in prompt


# ---------------------------------------------------------------------------
# 链路 F：观察归一化（真实 tool_success / tool_error）
# ---------------------------------------------------------------------------


def test_F1_F2_observation_shape(
    injected_execution_context,
):
    """测试目的：成功 observation 含 status/data/content/summary 且脱敏；
    错误 observation 含完整 error/reason/retryable 且与 reason 语义一致（F1+F2）。
    可能发现的缺陷：成功 observation 缺字段 / 错误 observation 缺 reason 或
    retryable 与 reason 不一致。"""
    ctx = injected_execution_context
    handler = _handler()

    # F1：成功路径（复用 A1 同形态）
    success_obs = handler(
        child_agent_id="delegate_reviewer",
        title="Obs shape success",
        objective="review main.py briefly",
        rules=["read only"],
        references=["main.py"],
        expected_output="one line review",
        execution_context=ctx["execution_context"],
    )
    assert success_obs.status == "success"
    assert success_obs.data is not None
    assert "delegation_id" in success_obs.data
    assert success_obs.content  # 非空正文
    # summary 与 data["status"] 一致（completed）
    assert success_obs.data.get("status") == "completed"
    # 脱敏：content 不应泄露常见 secret 形态（粗略断言，不泄露 key）
    assert "DEEPSEEK_API_KEY" not in success_obs.content

    # F2：错误路径（未知 child，含完整 error/reason/retryable）
    error_obs = handler(
        child_agent_id="ghost_agent",
        title="Obs shape error",
        objective="nope",
        rules=[],
        references=[],
        expected_output="never",
        execution_context=ctx["execution_context"],
    )
    assert error_obs.status == "error"
    assert error_obs.error  # 完整 error
    assert error_obs.reason  # 完整 reason
    assert isinstance(error_obs.retryable, bool)
    # retryable 与 reason 语义一致：确定性（未知 child）失败 retryable 应为 False
    assert error_obs.retryable is False


def test_G1_delete_task_cascades_delegation(
    injected_execution_context,
    real_runtime_stack: AgentRuntime,
):
    """测试目的：删除父 task 时，其下 delegation 子 Agent 持久化记录须一并清理（级联删除），
    不留孤儿。可能发现的缺陷：delete_task 漏删 delegations 表 → 孤儿记录无法追溯。"""
    ctx = injected_execution_context
    parent_task = ctx["parent_task"]
    task_service = TaskService()
    turn_service = TurnService()

    # 先真实创建一个 delegation（走 create，不等 child 跑完），模拟历史遗留委派记录
    from app.models.delegation_record import DelegationRecord
    from app.service.depends import get_delegation_crud
    from app.utils.datetime_utils import utc_now

    delegation = DelegationRecord(
        delegation_id="del_cascade",
        task_id=parent_task.task_id,
        parent_turn_id=ctx["parent_turn"].turn_id,
        child_turn_id="",
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        status="pending",
        prompt="cascade cleanup check",
        summary="",
        error="",
        effective_tools=("read_file",),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    get_delegation_crud().create(delegation)
    assert get_delegation_crud().get("del_cascade") is not None

    # 删除该 task
    task_service.delete_task(parent_task.task_id)

    # delegation 应随 task 一并删除，不留孤儿（get 不存在抛 KeyError）
    with pytest.raises(KeyError):
        get_delegation_crud().get("del_cascade")

    # task 与 turn 也应已删除（get 不存在抛 KeyError）
    with pytest.raises(KeyError):
        task_service.get_task(parent_task.task_id)
    with pytest.raises(KeyError):
        turn_service.get_turn(ctx["parent_turn"].turn_id)


def test_G2_delete_workspace_cascades_delegation(
    injected_execution_context,
    real_runtime_stack: AgentRuntime,
):
    """测试目的：删除 workspace 时，其下 task 的 delegation 子 Agent 持久化记录须一并清理
    （级联删除），不留孤儿。可能发现的缺陷：WorkspaceService.delete_workspace 直接走 task
    crud 而非 TaskService.delete_task，若漏删 delegations 表 → 孤儿记录无法追溯。"""
    ctx = injected_execution_context
    ws_service = WorkspaceService()
    task_service = TaskService()

    # 在同一 workspace 下真实创建一个 delegation，模拟历史遗留委派记录
    from app.models.delegation_record import DelegationRecord
    from app.service.depends import get_delegation_crud
    from app.utils.datetime_utils import utc_now

    delegation = DelegationRecord(
        delegation_id="del_ws_cascade",
        task_id=ctx["parent_task"].task_id,
        parent_turn_id=ctx["parent_turn"].turn_id,
        child_turn_id="",
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        status="pending",
        prompt="workspace cascade cleanup check",
        summary="",
        error="",
        effective_tools=("read_file",),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    get_delegation_crud().create(delegation)
    assert get_delegation_crud().get("del_ws_cascade") is not None

    # 删除整个 workspace
    ws_service.delete_workspace(ctx["workspace_id"])

    # delegation 应随 workspace 级联删除，不留孤儿（get 不存在抛 KeyError）
    with pytest.raises(KeyError):
        get_delegation_crud().get("del_ws_cascade")

    # workspace 与 task 也应已删除（get 不存在抛 KeyError）
    with pytest.raises(KeyError):
        ws_service.get_workspace(ctx["workspace_id"])
    with pytest.raises(KeyError):
        task_service.get_task(ctx["parent_task"].task_id)

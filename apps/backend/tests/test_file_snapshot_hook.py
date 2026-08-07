"""FileSnapshotHook（POST_TOOL_USE 内置 Hook）单元测试。

测试隔离：复用 ``tests/conftest.py`` 提供的 ``isolated_storage`` 夹具（独立 DB，
teardown 关闭 storage 并重置 service 依赖）。
"""

from app.hook.builtins.file_snapshot_hook import FileSnapshotHook
from app.hook.hook_context import HookContext
from app.hook.hook_event import HookEvent
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.tools.schemas.tool_observation import ToolObservation


def _observation(changes=None, status="success", tool_call_id="call1"):
    """构造带可选 changes 的成功/失败观察。"""
    data = {"changes": changes} if changes is not None else None
    return ToolObservation(
        tool_name="write_file",
        status=status,
        content="ok",
        tool_call_id=tool_call_id,
        data=data,
    )


def _context(observation, turn_id="turn1"):
    """构造 POST_TOOL_USE 上下文（经 from_locatable 提取 turn_id）。"""
    return HookContext.from_locatable(
        event=HookEvent.POST_TOOL_USE,
        locatable=_Locatable(turn_id),
        tool_name="write_file",
        tool_observation=observation,
    )


class _Locatable:
    """最小可定位对象，仅提供 turn_id 供 HookContext.from_locatable 提取。"""

    def __init__(self, turn_id):
        self.turn_id = turn_id


def test_records_snapshot_on_success_with_changes(isolated_storage):
    """成功观察且含 changes 时，逐文件落库反向操作快照。"""
    changes = [
        {
            "path": "a.txt",
            "status": "modified",
            "before": "line1\n",
            "after": "line1\nline2\n",
        }
    ]
    hook = FileSnapshotHook()
    result = hook.execute(_context(_observation(changes=changes)))

    assert result.decision.value == "allow"
    rows = FileSnapshotCrud().list_by_turn("turn1")
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_name == "write_file"
    assert row.tool_call_id == "call1"
    assert row.path == "a.txt"
    assert row.seq == 0


def test_skips_when_status_not_success(isolated_storage):
    """失败观察不落库。"""
    hook = FileSnapshotHook()
    hook.execute(_context(_observation(changes=[{"path": "a.txt"}], status="error")))

    assert FileSnapshotCrud().list_by_turn("turn1") == []


def test_skips_when_no_turn_id(isolated_storage):
    """无 turn_id 上下文不落库（回退快照需归属 turn）。"""
    changes = [{"path": "a.txt", "status": "created", "before": "", "after": "x"}]
    hook = FileSnapshotHook()
    hook.execute(_context(_observation(changes=changes), turn_id=None))

    assert FileSnapshotCrud().list_by_turn("turn1") == []


def test_skips_when_no_changes(isolated_storage):
    """观察无 changes（如 execute_terminal）不落库。"""
    hook = FileSnapshotHook()
    hook.execute(_context(_observation(changes=None)))

    assert FileSnapshotCrud().list_by_turn("turn1") == []


def test_returns_allow_on_record_exception(isolated_storage, monkeypatch):
    """采集内部异常被吞掉并仍返回 allow，不冒泡阻断主流程。"""
    hook = FileSnapshotHook()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(FileSnapshotCrud, "next_seq", _boom)

    changes = [{"path": "a.txt", "status": "created", "before": "", "after": "x"}]
    result = hook.execute(_context(_observation(changes=changes)))

    assert result.decision.value == "allow"


def test_pre_tool_use_modified_arguments_applies(isolated_storage, tmp_path):
    """回归：PRE_TOOL_USE 返回 modified_arguments 时，调度器改写参数后执行。

    锁定 ``tool_scheduler.execute`` 的 ``dataclasses.replace`` 路径未被误删 import，
    否则改写分支会抛 ``NameError``。
    """
    from app.hook.hook_base import HookBase
    from app.hook.hook_event import HookDecision
    from app.hook.hook_registry import get_hook_registry
    from app.hook.hook_result import HookResult
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.write_file import build_write_file_definition
    from app.tools.tool_registry import ToolRegistry

    class _RewriteHook(HookBase):
        def __init__(self):
            super().__init__(event=HookEvent.PRE_TOOL_USE, matcher=None)

        def execute(self, context: HookContext) -> HookResult:
            args = dict(context.tool_arguments or {})
            args["path"] = "rewritten.txt"
            return HookResult(decision=HookDecision.ALLOW, modified_arguments=args)

    get_hook_registry().register(_RewriteHook())

    ws = tmp_path / "ws"
    ws.mkdir()
    registry = ToolRegistry()
    registry.register(build_write_file_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=ws, turn_id="turn1"
    )
    obs = scheduler.execute(
        ToolCall(
            tool_name="write_file",
            arguments={"path": "original.txt", "content": "hello"},
            call_id="c1",
        ),
        execution_context=ctx,
    )
    assert obs.status == "success"
    assert (ws / "rewritten.txt").exists()
    assert not (ws / "original.txt").exists()

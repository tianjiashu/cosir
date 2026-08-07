"""FileSnapshotHook 补充测试：装配契约、多文件/多次落库、op_json 可回放、端到端链路。

与 ``tests/test_file_snapshot_hook.py`` 的分工：那份覆盖 Hook 自身四个跳过分支与
异常兜底；本文件补齐复核中发现的覆盖缺口——
1. 装配契约：``bootstrap_hooks`` 是否真的把 Hook 挂到 POST_TOOL_USE（改动的核心诉求，
   Hook 未注册则整条快照链路静默失效，而单测直接 new Hook 无法发现）。
2. 落库正确性：多文件顺序、seq 跨多次调用递增、additions/deletions 精确值。
3. ``op_json`` 反向语义可回放（``PatchOperation(**json.loads(...))`` 重建）。
4. 端到端：经 ``ToolScheduler.execute`` 真实触发 POST_TOOL_USE 后产生快照。
5. 边界：``changes=[]`` 空列表分支（原测试未覆盖，coverage 缺失行）。

测试隔离：复用 ``tests/conftest.py`` 的 ``isolated_storage``（独立 DB + 重置 Hook 注册表）。
"""

import json

import pytest

from app.hook.builtins.file_snapshot_hook import FileSnapshotHook
from app.hook.hook_context import HookContext
from app.hook.hook_event import HookEvent
from app.hook.hook_registry import HookRegistry, get_hook_registry
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.tools.schemas.tool_observation import ToolObservation
from app.tools.tool_handler.patch.patch_parser import PatchOperation


class _Locatable:
    """最小可定位对象，仅提供 turn_id 供 HookContext.from_locatable 提取。"""

    def __init__(self, turn_id):
        self.turn_id = turn_id


def _observation(changes=None, status="success", tool_call_id="call1"):
    """构造带可选 changes 的观察；changes 为 None 时 data 也为 None。"""
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


# --------------------------------------------------------------------------- #
# 1. 装配契约
# --------------------------------------------------------------------------- #


def test_bootstrap_registers_file_snapshot_hook_on_post_tool_use():
    """测试目的：bootstrap_hooks 必须把 FileSnapshotHook 挂到 POST_TOOL_USE。

    可能发现的缺陷：漏调 _register_file_snapshot_hook、注册到错误事件、或被
    bootstrap 内 try/except 静默吞掉 —— 此类缺陷会让整条快照链路静默失效，
    而「直接 new Hook」的单测永远发现不了。
    """
    from app.hook.builtins.bootstrap_hooks import bootstrap_hooks

    registry = HookRegistry()
    bootstrap_hooks(registry)

    post_hooks = registry.resolve_for(HookEvent.POST_TOOL_USE)
    assert [type(h).__name__ for h in post_hooks].count("FileSnapshotHook") == 1, (
        "FileSnapshotHook 必须且仅注册一次到 POST_TOOL_USE"
    )


def test_hook_event_and_matcher_contract():
    """测试目的：Hook 固化 event=POST_TOOL_USE 且 matcher=None（对所有工具生效）。

    可能发现的缺陷：漏调 super().__init__ 导致 matches() 访问 _compiled 抛
    AttributeError（Hook 永不执行）；或误设 matcher 使非匹配工具被静默跳过。
    """
    hook = FileSnapshotHook()

    assert hook.event is HookEvent.POST_TOOL_USE
    # matcher=None 语义：对任意工具名都应 matches -> True
    assert hook.matches(_context(_observation(changes=[]), turn_id="turn1")) is True
    ctx_other_tool = HookContext(
        event=HookEvent.POST_TOOL_USE, tool_name="execute_terminal", turn_id="t"
    )
    assert hook.matches(ctx_other_tool) is True


def test_isolated_storage_fixture_initializes_registry(isolated_storage):
    """测试目的：conftest 的 isolated_storage 确实完成 Hook 注册表初始化。

    可能发现的缺陷：夹具漏调 initialize_hook_registry 时，get_hook_registry 会
    fail-fast 抛 RuntimeError，或残留上个用例的注册表造成跨用例污染。
    """
    registry = get_hook_registry()
    names = [type(h).__name__ for h in registry.resolve_for(HookEvent.POST_TOOL_USE)]
    assert "FileSnapshotHook" in names


# --------------------------------------------------------------------------- #
# 2. 落库正确性：多文件 / seq 递增 / diff 统计
# --------------------------------------------------------------------------- #


def test_records_one_row_per_changed_file_in_order(isolated_storage):
    """测试目的：多文件 changes 应逐文件落库，seq 从 next_seq 起按顺序递增。

    可能发现的缺陷：只落第一个文件、seq 全部相同（覆盖写）、或 offset 未累加
    导致回退时顺序错乱。
    """
    changes = [
        {"path": "a.txt", "status": "modified", "before": "x\n", "after": "y\n"},
        {"path": "b.txt", "status": "added", "before": "", "after": "new\n"},
        {"path": "c.txt", "status": "deleted", "before": "old\n", "after": ""},
    ]
    FileSnapshotHook().execute(_context(_observation(changes=changes)))

    rows = sorted(FileSnapshotCrud().list_by_turn("turn1"), key=lambda r: r.seq)
    assert [r.path for r in rows] == ["a.txt", "b.txt", "c.txt"]
    assert [r.seq for r in rows] == [0, 1, 2]


def test_seq_continues_across_multiple_tool_calls(isolated_storage):
    """测试目的：同一 turn 内多次工具调用，seq 必须全局连续递增不冲突。

    可能发现的缺陷：每次调用都从 0 开始（未用 next_seq），导致同 turn 内 seq
    重复、回退逆序应用时错乱或主键/顺序冲突。
    """
    hook = FileSnapshotHook()
    hook.execute(
        _context(_observation(changes=[{"path": "a.txt", "status": "added", "after": "1\n"}]))
    )
    hook.execute(
        _context(_observation(changes=[{"path": "b.txt", "status": "added", "after": "2\n"}]))
    )

    rows = sorted(FileSnapshotCrud().list_by_turn("turn1"), key=lambda r: r.seq)
    assert [r.seq for r in rows] == [0, 1]
    assert [r.path for r in rows] == ["a.txt", "b.txt"]


def test_snapshots_are_scoped_per_turn(isolated_storage):
    """测试目的：不同 turn 的快照互相隔离，各自 seq 独立从 0 起。

    可能发现的缺陷：turn_id 未正确透传/落库，导致回退时误还原别的 turn 的文件。
    """
    hook = FileSnapshotHook()
    changes = [{"path": "a.txt", "status": "added", "after": "1\n"}]
    hook.execute(_context(_observation(changes=changes), turn_id="turnA"))
    hook.execute(_context(_observation(changes=changes), turn_id="turnB"))

    crud = FileSnapshotCrud()
    rows_a = crud.list_by_turn("turnA")
    rows_b = crud.list_by_turn("turnB")
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0].seq == 0 and rows_b[0].seq == 0
    assert rows_a[0].turn_id == "turnA" and rows_b[0].turn_id == "turnB"


@pytest.mark.parametrize(
    ("status", "before", "after", "expected_add", "expected_del"),
    [
        ("added", "", "l1\nl2\n", 2, 0),
        ("deleted", "l1\nl2\nl3\n", "", 0, 3),
        ("modified", "l1\nl2\n", "l1\nCHANGED\n", 1, 1),
    ],
)
def test_diff_stats_are_precise_per_status(
    isolated_storage, status, before, after, expected_add, expected_del
):
    """测试目的：各 status 下 additions/deletions 精确值（不是「非零」弱断言）。

    可能发现的缺陷：added/deleted 分支增删算反、modified 未做逐行 diff 而是整文件
    计数，导致变更集面板行数统计错误。
    """
    changes = [{"path": "f.txt", "status": status, "before": before, "after": after}]
    FileSnapshotHook().execute(_context(_observation(changes=changes)))

    row = FileSnapshotCrud().list_by_turn("turn1")[0]
    assert (row.additions, row.deletions) == (expected_add, expected_del)


def test_action_records_forward_operation_type(isolated_storage):
    """测试目的：action 落的是「正向」操作类型（回退侧据此解释 op_json）。

    可能发现的缺陷：误把反向操作类型写进 action，导致回退方向判断颠倒。
    """
    changes = [{"path": "n.txt", "status": "added", "before": "", "after": "x\n"}]
    FileSnapshotHook().execute(_context(_observation(changes=changes)))

    row = FileSnapshotCrud().list_by_turn("turn1")[0]
    assert row.action == "add"


# --------------------------------------------------------------------------- #
# 3. op_json 可回放（反向语义）
# --------------------------------------------------------------------------- #


def test_op_json_rebuildable_and_reverses_add_to_delete(isolated_storage):
    """测试目的：新增文件的 op_json 必须能重建为 PatchOperation 且为 DELETE。

    可能发现的缺陷：枚举未降级为字符串导致 json 序列化失败/反序列化后类型错误；
    反向方向写错（add 的反向应是删除），回退时不但没还原反而二次写入。
    """
    changes = [{"path": "n.txt", "status": "added", "before": "", "after": "hello\n"}]
    FileSnapshotHook().execute(_context(_observation(changes=changes)))

    row = FileSnapshotCrud().list_by_turn("turn1")[0]
    payload = json.loads(row.op_json)
    rebuilt = PatchOperation(**payload)  # 回退侧真实重建路径
    # PatchOperation 是普通 dataclass，不会把字符串转回枚举；落库的是 .value 字符串
    assert rebuilt.operation == "delete"
    assert rebuilt.file_path == "n.txt"


def test_op_json_reverses_delete_to_add_with_original_content(isolated_storage):
    """测试目的：删除文件的反向操作应为 ADD 且携带原始 before 全文（精确还原）。

    可能发现的缺陷：before 全文丢失（只存 hunk 摘要），回退重建出空文件或截断内容。
    """
    original = "line1\nline2\n"
    changes = [{"path": "d.txt", "status": "deleted", "before": original, "after": ""}]
    FileSnapshotHook().execute(_context(_observation(changes=changes)))

    row = FileSnapshotCrud().list_by_turn("turn1")[0]
    rebuilt = PatchOperation(**json.loads(row.op_json))
    assert rebuilt.operation == "add"
    assert rebuilt.content == original


def test_op_json_is_valid_json_for_non_ascii_paths(isolated_storage):
    """测试目的：中文路径/内容的 op_json 仍是合法 JSON 且原样保留（ensure_ascii=False）。

    可能发现的缺陷：编码处理不当导致 op_json 出现乱码或反序列化失败，中文项目回退失效。
    """
    changes = [{"path": "文档/说明.md", "status": "added", "before": "", "after": "内容\n"}]
    FileSnapshotHook().execute(_context(_observation(changes=changes)))

    row = FileSnapshotCrud().list_by_turn("turn1")[0]
    assert row.path == "文档/说明.md"
    assert PatchOperation(**json.loads(row.op_json)).file_path == "文档/说明.md"


# --------------------------------------------------------------------------- #
# 4. 边界与失败安全
# --------------------------------------------------------------------------- #


def test_skips_when_changes_is_empty_list(isolated_storage):
    """测试目的：changes 为空列表（假值）走跳过分支，不落库、不产生空 seq 占位。

    可能发现的缺陷：用 `is None` 而非真值判断，空列表时仍进入 _record，
    白白占用 seq 或写入空记录。（此前 coverage 未覆盖的分支）
    """
    result = FileSnapshotHook().execute(_context(_observation(changes=[])))

    assert result.decision.value == "allow"
    assert FileSnapshotCrud().list_by_turn("turn1") == []


def test_skips_when_observation_is_none():
    """测试目的：tool_observation 为 None（非工具事件误触发）时安全跳过。

    可能发现的缺陷：未判空直接取 .status 抛 AttributeError，污染主流程日志。
    """
    ctx = HookContext(event=HookEvent.POST_TOOL_USE, tool_name="x", turn_id="turn1")
    assert FileSnapshotHook().execute(ctx).decision.value == "allow"


def test_malformed_change_is_swallowed_and_allows(isolated_storage):
    """测试目的：changes 字段类型非法（path 非 str）时吞异常并返回 allow，不落库。

    可能发现的缺陷：build_forward_operations 抛 TypeError 未被 execute 捕获，
    冒泡穿透 Hook 破坏工具主流程（违反失败安全语义）。
    """
    changes = [{"path": 123, "status": "modified", "before": "a", "after": "b"}]
    result = FileSnapshotHook().execute(_context(_observation(changes=changes)))

    assert result.decision.value == "allow"
    assert FileSnapshotCrud().list_by_turn("turn1") == []


def test_save_failure_does_not_break_flow(isolated_storage, monkeypatch):
    """测试目的：落库 save 抛异常时仍返回 allow（区别于既有 next_seq 失败用例）。

    可能发现的缺陷：异常捕获范围只包住 next_seq 未包住 save 循环，
    部分写入后异常冒泡阻断工具主流程。
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated save failure")

    monkeypatch.setattr(FileSnapshotCrud, "save", _boom)

    changes = [{"path": "a.txt", "status": "added", "before": "", "after": "x\n"}]
    result = FileSnapshotHook().execute(_context(_observation(changes=changes)))

    assert result.decision.value == "allow"


# --------------------------------------------------------------------------- #
# 5. 端到端：经 ToolScheduler 真实触发
# --------------------------------------------------------------------------- #


def test_end_to_end_scheduler_write_file_produces_snapshot(isolated_storage, tmp_path):
    """测试目的：经 ToolScheduler.execute 走真实 POST_TOOL_USE，产生可回退快照。

    这是本次改动的核心契约（私有方法 → Hook 后链路仍连通）。
    可能发现的缺陷：safe_fire 未在成功路径触发、HookContext 未透传 turn_id、
    或 observation.data 中 changes 键名变更 —— 任一都会让回退能力静默丢失。
    """
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.write_file import build_write_file_definition
    from app.tools.tool_registry import ToolRegistry

    ws = tmp_path / "ws"
    ws.mkdir()
    registry = ToolRegistry()
    registry.register(build_write_file_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=ws, turn_id="turnE2E"
    )

    obs = scheduler.execute(
        ToolCall(
            tool_name="write_file",
            arguments={"path": "hello.txt", "content": "hi\n"},
            call_id="c1",
        ),
        execution_context=ctx,
    )

    assert obs.status == "success"
    rows = FileSnapshotCrud().list_by_turn("turnE2E")
    assert len(rows) == 1, "写文件成功后必须产生恰好一条回退快照"
    row = rows[0]
    assert row.path == "hello.txt"
    assert row.tool_name == "write_file"
    assert row.tool_call_id == "c1"
    # 反向操作应能重建，且为删除（新建文件的回退 = 删掉它）
    assert PatchOperation(**json.loads(row.op_json)).operation == "delete"


def test_end_to_end_failed_tool_produces_no_snapshot(isolated_storage, tmp_path):
    """测试目的：工具执行失败（参数非法）时不得产生快照。

    注意 write_file 的 content 有默认值，缺 content 仍是合法调用；此处用
    缺失必填 path 触发参数校验失败（该路径在 PRE_TOOL_USE 之前短路，
    连 POST_TOOL_USE 都不会触发）。

    可能发现的缺陷：POST_TOOL_USE 在失败路径也落库，导致回退时对未发生的
    改动执行反向操作，反而破坏用户文件。
    """
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.write_file import build_write_file_definition
    from app.tools.tool_registry import ToolRegistry

    ws = tmp_path / "ws"
    ws.mkdir()
    registry = ToolRegistry()
    registry.register(build_write_file_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=ws, turn_id="turnFail"
    )

    obs = scheduler.execute(
        ToolCall(tool_name="write_file", arguments={"content": "no path"}, call_id="c2"),
        execution_context=ctx,
    )

    assert obs.status == "error"
    assert FileSnapshotCrud().list_by_turn("turnFail") == []

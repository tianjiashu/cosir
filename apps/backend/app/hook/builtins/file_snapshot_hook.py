"""工具执行成功后采集文件回退快照的 Hook（POST_TOOL_USE）。

单一职责：在文件类工具成功执行后，从工具观察的 ``data["changes"]`` 采集反向
操作快照，供 Turn 回退按 turn 精准还原。它是 ``POST_TOOL_USE`` 的一个内置订阅，
与既有的审计切面并行触发，互不干扰。

职责边界：
- 负责：成功判定、run_id / task_id 判定、changes 提取、正向 V4A 构造、反转为
  反向操作、逐文件落库 file_snapshots、采集异常本地吞 + warning。
- 不负责：工具执行编排、参数校验、文件状态协调（均归 ToolScheduler / 各 guard）。

失败安全语义：采集失败只记 warning 日志，不阻断工具主流程（方案 §六 可重入要求）；
Hook 异常或被 HookInterceptor.safe_fire 兜底为 ALLOW 均不阻断主流程。
"""

import dataclasses
import enum
import json

from app.config.logging.logger import log
from app.hook.hook_base import HookBase
from app.hook.hook_context import HookContext
from app.hook.hook_event import HookEvent
from app.hook.hook_result import HookResult
from app.models.file_snapshot_record import FileSnapshotRecord
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.core.tools.schemas import ToolObservation
from app.core.tools.tool_handler.patch.patch_diff import FileDiffResult, build_diff_stats
from app.core.tools.tool_handler.patch.patch_parser import PatchOperation
from app.core.tools.tool_handler.patch.v4a_reverse import (
    build_forward_operations,
    reverse_v4a_operation,
)


class FileSnapshotHook(HookBase):
    """工具成功后采集文件回退快照的内置 Hook。

    挂载于 ``HookEvent.POST_TOOL_USE``，对所有工具触发；内部自行判定是否产生
    可落库快照（仅成功、带 run_id / task_id、且 observation.data 含 changes 的文件工具）。
    """

    def __init__(self) -> None:
        """构造文件快照 Hook。

        参数:
            无。

        返回:
            无。

        异常:
            ValueError: 基类若传入非法 matcher（本处固定 ``matcher=None``，正常不抛）。

        副作用:
            固化基类 ``event=POST_TOOL_USE`` / ``matcher=None`` 只读属性（必须
            调用 ``super().__init__``，否则 ``matches()`` 访问 ``_compiled`` 抛
            ``AttributeError``，导致 Hook 永不执行）。
        """
        super().__init__(event=HookEvent.POST_TOOL_USE, matcher=None)

    def execute(self, context: HookContext) -> HookResult:
        """采集文件回退快照。

        流程：非成功观察 → 跳过；无 run_id 或 task_id → 跳过（task_id 缺失时
        放弃采集，避免快照落入空串归属的 seq 命名空间）；无 ``observation.data``
        → 跳过；无 ``changes`` → 跳过；构造正向 V4A、反转为反向操作、逐文件落库。
        任何异常 → warning 日志 + ALLOW（不阻断主流程）。

        参数:
            context: 运行时注入的 ``HookContext``，含 ``tool_name`` /
                ``tool_observation`` / ``run_id`` / ``task_id``。

        返回:
            HookResult.allow()：本 Hook 永不阻断主流程（快照是旁路数据副作用，
            失败安全）。

        异常:
            不向外抛出：所有异常在内部吞掉并记为 warning 日志，保证 ALLOW。

        副作用:
            向 ``file_snapshots`` 表写入 0~N 条反向操作记录（每个变更文件一条），
            每条记录同时写入该次变更相对上一次的 diff 增删行数。
        """
        observation = context.tool_observation
        if observation is None or observation.status != "success":
            return HookResult.allow()
        if not context.run_id or not context.task_id:
            return HookResult.allow()
        data = observation.data
        if not data:
            return HookResult.allow()
        changes = data.get("changes")
        if not changes:
            return HookResult.allow()

        tool_name = context.tool_name or ""
        try:
            # HookContext 携带的 task_id/run_id 为字符串标识，落库快照层要求整数主键。
            task_id = int(context.task_id)
            run_id = int(context.run_id)
            self._record(observation, tool_name, task_id, run_id)
        except Exception:
            log.warning(
                "file_snapshot_record_failed",
                extra={
                    "msg": "文件快照采集失败，回退时可能丢失该次文件改动还原能力",
                    "data": {
                        "task_id": context.task_id,
                        "run_id": context.run_id,
                        "tool_name": tool_name,
                    },
                },
                exc_info=True,
            )
        return HookResult.allow()

    def _record(
        self,
        observation: ToolObservation,
        tool_name: str,
        task_id: int,
        run_id: int,
    ) -> None:
        """把一次工具观察的 changes 落库为反向操作快照。

        参数:
            observation: 归一化后的工具观察结果（提供 ``tool_call_id`` 与 ``data["changes"]``）。
            tool_name: 被执行工具名（作为快照 ``tool_name``）。
            task_id: 任务标识（快照归属任务，seq 命名空间边界）。
            run_id: 轮次标识（快照归属的 turn）。

        返回:
            无。

        异常:
            Exception: 采集/落库任何环节异常向上冒泡，由 ``execute`` 统一吞掉为 warning。

        副作用:
            向 ``file_snapshots`` 表写入 0~N 条反向操作记录。
        """
        data = observation.data or {}
        changes = data.get("changes")
        if not isinstance(changes, list):
            log.warning(
                "file_snapshot_changes_invalid",
                extra={
                    "msg": "工具观察 data.changes 非 list，跳过文件快照采集",
                    "data": {"task_id": task_id, "run_id": run_id, "tool_name": tool_name},
                },
            )
            return
        forward_ops = build_forward_operations(changes)
        diff_stats: list[tuple[int, int]] = _change_diff_stats(changes)
        crud = FileSnapshotCrud()
        # seq 由 save_batch_with_sequence 在进程级锁内原子分配（同一次变更的所有
        # 反向操作占用连续 seq 区间），避免同一 task 下并行工具调用并发快照采集
        # 读到相同 MAX(seq) 而产生重复 seq（此前 next_seq+逐条 save 存在该竞态）。
        crud.save_batch_with_sequence(
            task_id,
            [
                FileSnapshotRecord(
                    task_id=task_id,
                    run_id=run_id,
                    tool_name=tool_name,
                    tool_call_id=observation.tool_call_id,
                    path=forward.file_path,
                    action=forward.operation.value,
                    op_json=_reverse_op_to_json(reverse_v4a_operation(forward)),
                    additions=diff_stats[offset][0] if offset < len(diff_stats) else 0,
                    deletions=diff_stats[offset][1] if offset < len(diff_stats) else 0,
                )
                for offset, forward in enumerate(forward_ops)
            ],
        )


def _change_diff_stats(changes: list[dict]) -> list[tuple[int, int]]:
    """计算采集快照中每个文件的 diff 增删行数。

    复用 ``patch_diff.build_diff_stats`` 的 difflib 逐行统计，避免重复实现差异算法：
    - ``added``：全部 after 行计为新增，deletions 为 0。
    - ``deleted``：全部 before 行计为删除，additions 为 0。
    - ``modified``：按 before/after 逐行 diff 统计增删。
    - ``moved``：计 0/0。

    参数:
        changes: ``data["changes"]`` 中的单文件变更字典列表，每个含
            ``path`` / ``new_path`` / ``status`` / ``before`` / ``after``。

    返回:
        与 ``changes`` 顺序一致的 ``(additions, deletions)`` 二元组列表。

    异常:
        无。

    副作用:
        无。
    """
    results = [
        FileDiffResult(
            path=change.get("path", ""),
            status=change.get("status", "modified"),
            before=change.get("before", ""),
            after=change.get("after", ""),
            new_path=change.get("new_path"),
        )
        for change in changes
    ]
    stats = build_diff_stats(results)
    return [
        (int(f.get("insertions", 0)), int(f.get("deletions", 0))) for f in stats.get("files", [])
    ]


def _reverse_op_to_json(reverse_op: "PatchOperation") -> str:
    """把反向 PatchOperation 投影为可 JSON 序列化的字典字符串。

    采用 ``dataclasses.asdict`` 保留与 ``PatchOperation`` dataclass 字段名/嵌套形状
    兼容的结构：``operation`` 落为 ``OperationType`` 的 ``value`` 字符串、``hunks``
    展开为 ``{"lines": [{"prefix", "content"}], "context_hint": null}``，确保
    ``json.dumps`` 可直接序列化。回退侧不能 ``PatchOperation(**data)`` 直接重建
    （``operation`` 为字符串、``hunks`` 内 ``HunkLine`` 为 dict，直接构造的对象
    不可用），须按 ``service.task.change_set.snapshot_patch`` 的方式手动重建：
    ``OperationType(value)`` 转枚举、逐层构造 ``Hunk``/``HunkLine`` 后再使用。

    参数:
        reverse_op: 已构造的反向 PatchOperation（含 OperationType 枚举与嵌套 Hunk）。

    返回:
        JSON 字符串；结构与 ``PatchOperation`` 构造参数兼容（枚举已落为 value）。

    异常:
        无。

    副作用:
        无。
    """

    def _enum_to_value(obj: object) -> object:
        """把对象树中的 Enum 递归转为 ``value`` 字符串，使 asdict 结果可 JSON 序列化。

        参数:
            obj: ``dataclasses.asdict`` 产出的任意嵌套对象（Enum / dict / list / 叶子）。

        返回:
            同构对象树，Enum 节点替换为 ``value`` 字符串。

        异常:
            无。

        副作用:
            无。
        """
        if isinstance(obj, enum.Enum):
            return obj.value
        if isinstance(obj, dict):
            return {k: _enum_to_value(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_enum_to_value(v) for v in obj]
        return obj

    raw = dataclasses.asdict(reverse_op)
    serializable = _enum_to_value(raw)
    return json.dumps(serializable, ensure_ascii=False)

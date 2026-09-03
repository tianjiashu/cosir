"""V4A 文件变更反向操作构造。

把一次 turn 内文件工具（write_file / patch / delete / move）成功执行产生的
``display_data["changes"]``（采集层事实快照）转换为「反向 V4A 操作」列表，落库后
供 task 级变更集（``service.task.change_set``）用 ``apply_all_with_diff`` 逆向应用，
将文件还原到该变更执行前的状态。

设计边界：
- 只做「采集快照 → 反向 PatchOperation」的纯转换，不读写文件、不关心工具权限。
- 反向语义严格基于采集层 diff 对调（before/after / 路径对调），不依赖原始 patch hunks，
  避免「原始 hunk 已无法在逆向时匹配」的脆弱性（见方案 §六 风险说明）。
- 反向操作与 ``apply_all_with_diff`` 共用 ``PatchOperation`` 契约，复用成熟应用逻辑。
"""

from app.core.tools.tool_handler.patch.patch_parser import (
    Hunk,
    HunkLine,
    OperationType,
    PatchOperation,
    hunk_content,
)


def build_forward_operations(changes: list[dict]) -> list[PatchOperation]:
    """从采集层变更快照构造「正向」V4A 操作列表（Turn 回退文件快照采集主链路入口）。

    参数:
        changes: ``display_data["changes"]`` 中的单文件变更字典列表，每个含
            ``path`` / ``new_path`` / ``status`` / ``before`` / ``after``。

    返回:
        与采集快照一致的「正向」PatchOperation 列表（added/modified/deleted/moved）。

    异常:
        无。

    副作用:
        无。
    """
    operations: list[PatchOperation] = []
    for change in changes:
        status = change.get("status")
        path = change.get("path")
        new_path = change.get("new_path")
        before = change.get("before", "")
        after = change.get("after", "")
        # 采集层约定的字段应为字符串；显式校验（不用 assert，避免 python -O 下被剥离
        # 导致类型错误静默通过）。非法即抛 TypeError，由采集侧统一捕获记 exception 日志。
        if not isinstance(path, str):
            raise TypeError(f"change.path must be str, got {type(path).__name__}")
        if new_path is not None and not isinstance(new_path, str):
            raise TypeError(f"change.new_path must be str or None, got {type(new_path).__name__}")
        if not isinstance(before, str):
            raise TypeError(f"change.before must be str, got {type(before).__name__}")
        if not isinstance(after, str):
            raise TypeError(f"change.after must be str, got {type(after).__name__}")
        if status == "added":
            operations.append(
                PatchOperation(
                    operation=OperationType.ADD,
                    file_path=path,
                    hunks=[_content_to_hunk(after, prefix="+")],
                    content=after,
                )
            )
        elif status == "deleted":
            # DELETE 需把 before 全文带入 hunks 与 content，供反向（ADD）重建文件内容。
            # content 保留原始字节（含尾换行 / CRLF / BOM），回退时绕过 hunk 拼接，
            # 由 atomic_write_text 精确还原，避免 splitlines 重建丢失尾换行。
            operations.append(
                PatchOperation(
                    operation=OperationType.DELETE,
                    file_path=path,
                    hunks=[_content_to_hunk(before, prefix="+")],
                    content=before,
                )
            )
        elif status == "moved":
            operations.append(
                PatchOperation(
                    operation=OperationType.MOVE,
                    file_path=path,
                    new_path=new_path,
                )
            )
        else:  # modified
            operations.append(
                PatchOperation(
                    operation=OperationType.UPDATE,
                    file_path=path,
                    hunks=[_before_after_to_hunk(before, after)],
                    # reverse_content 携带 before 原文（含尾换行），仅供 reverse_v4a_operation
                    # 读取以构造精确的内容覆盖，避免反向 UPDATE 走 fuzzy 在「after 为空」
                    # （整文件清空）等场景失败。
                    reverse_content=before,
                )
            )
    return operations


def reverse_v4a_operation(forward: PatchOperation) -> PatchOperation:
    """把单个正向 PatchOperation 反向为「还原」操作。

    反向语义（基于采集 diff 对调，不依赖原始 patch hunks）：
    - ADD  → DELETE（新建的文件应被删除）。
    - DELETE → ADD（删除的文件应被重建，content = 采集的 before 全文）。
    - UPDATE → UPDATE（after 换回 before：查找文本用原 after，替换文本用原 before）。
    - MOVE  → MOVE（路径对调：source → 原 new_path，new_path → 原 file_path）。

    参数:
        forward: 正向 PatchOperation（来自 ``build_forward_operations``）。

    返回:
        反向 PatchOperation，可经 ``apply_all_with_diff`` 将文件还原到执行前。

    异常:
        无。

    副作用:
        无。
    """
    op = forward.operation
    if op == OperationType.ADD:
        return PatchOperation(
            operation=OperationType.DELETE,
            file_path=forward.file_path,
        )
    if op == OperationType.DELETE:
        # 重建被删文件：content 来自采集的 before 全文（精确还原，含尾换行 / CRLF / BOM）。
        # 优先用 forward.content（build_forward_operations 已带入完整 before）；Hunk 仅作为
        # 兼容/回显冗余。content 缺失时回退到从 hunks 拼接（不保留尾换行，仅防御）。
        content = (
            forward.content if forward.content is not None else hunk_content(forward.hunks, "+")
        )
        return PatchOperation(
            operation=OperationType.ADD,
            file_path=forward.file_path,
            hunks=[_content_to_hunk(content, prefix="+")],
            content=content,
        )
    if op == OperationType.MOVE:
        return PatchOperation(
            operation=OperationType.MOVE,
            file_path=forward.new_path or forward.file_path,
            new_path=forward.file_path,
        )
    # UPDATE：after ↔ before 对调；content 携 forward.reverse_content（before 原文），
    # 供 apply_all_with_diff 整文件覆盖还原，正确处理「after 为空（整文件清空）/
    # before 为空（整文件新增）」等场景，避免 fuzzy 空 search 跳过导致虚假成功。
    before = (
        forward.reverse_content
        if forward.reverse_content is not None
        else hunk_content(forward.hunks, "+")
    )
    return PatchOperation(
        operation=OperationType.UPDATE,
        file_path=forward.file_path,
        # 保留正向 hunks 仅供审计/回显，apply 侧只依赖 content 做整文件覆盖还原，
        # 不读取 hunks（patch_apply UPDATE 分支 content 非 None 时直接返回）。
        # 注意：未来若对反向 UPDATE 调 validate_all，其 hunk fuzzy 校验会用正向方向
        # 的 hunks 匹配磁盘 after 态，可能失败；当前 revert_file 不调 validate_all。
        hunks=forward.hunks,
        content=before,
    )


def _content_to_hunk(content: str, *, prefix: str) -> Hunk:
    """把纯文本内容构造为单一 Hunk（所有行统一前缀）。

    参数:
        content: 文本内容。
        prefix: 行前缀（'+' 新增 / ' ' 上下文）。

    返回:
        含全部行的 Hunk。
    """
    lines = content.splitlines(keepends=True)
    if not lines and content == "":
        lines = []
    hunk_lines = [HunkLine(prefix=prefix, content=line.rstrip("\n")) for line in lines]
    return Hunk(lines=hunk_lines)


def _before_after_to_hunk(before: str, after: str) -> Hunk:
    """把 before/after 全量内容构造为 UPDATE hunk（删旧行 + 增新行）。

    采用标准 unified diff 语义：先列出 before 全量的 ``-`` 行，再列出 after 全量的
    ``+`` 行（不加重复上下文行，避免 ``_hunk_search`` 拼接出重复文本导致无法匹配）。
    ``apply_all_with_diff`` 的 ``_hunk_search`` / ``_hunk_replace`` 分别取
    ``' '+' '-'`` 与 ``' '+' '+'`` 行，此处无上下文行，查找文本即 before 全文、
    替换文本即 after 全文，可直接被 fuzzy 匹配。

    参数:
        before: 变更前全文。
        after: 变更后全文。

    返回:
        含 ``-before全行`` 与 ``+after全行`` 的 Hunk。
    """
    before_lines = before.splitlines(keepends=False)
    after_lines = after.splitlines(keepends=False)
    hunk_lines: list[HunkLine] = [HunkLine(prefix="-", content=line) for line in before_lines]
    hunk_lines.extend(HunkLine(prefix="+", content=line) for line in after_lines)
    return Hunk(lines=hunk_lines)

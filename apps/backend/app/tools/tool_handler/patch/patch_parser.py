"""patch 格式解析（仅 V4A）。

把 V4A 格式 patch 文本解析为 ``PatchOperation``（Add/Update/Delete/Move），
供 ``patch_apply`` 做两阶段校验与应用。hunk 上下文匹配复用
``fuzzy_match.fuzzy_find_and_replace``，不自写行邻接匹配。

设计边界：
- 只解析，不做任何文件读写或落盘。
- V4A 解析逻辑移植自 Hermes ``patch_parser.parse_v4a_patch``。
- 输入仅支持 V4A（Hermes 风格）；git unified diff 输入解析已移除（对齐 Hermes）。
"""

import re
from dataclasses import dataclass, field
from enum import Enum


class OperationType(Enum):
    """patch 操作类型。"""

    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass
class HunkLine:
    """patch hunk 中的单行。"""

    prefix: str  # ' '（上下文）/ '-'（删除）/ '+'（新增）
    content: str


@dataclass
class Hunk:
    """patch 中一组相邻变更。"""

    context_hint: str | None = None
    lines: list[HunkLine] = field(default_factory=list)


@dataclass
class PatchOperation:
    """一个统一的 patch 操作。"""

    operation: OperationType
    file_path: str
    new_path: str | None = None  # Move 操作的目标路径
    hunks: list[Hunk] = field(default_factory=list)
    content: str | None = None  # Add 操作的完整内容（由 hunks 推导）


def parse_v4a_patch(patch_content: str) -> tuple[list[PatchOperation], str | None]:
    """解析 V4A 格式 patch。

    参数:
        patch_content: V4A 格式 patch 文本。

    返回:
        ``(operations, None)`` 表示成功；``([], error)`` 表示解析失败。空 patch
        返回 ``([], None)``（由调用方决定如何处理）。

    异常:
        无。

    副作用:
        无。
    """

    lines = patch_content.splitlines()
    operations: list[PatchOperation] = []

    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        if "*** Begin Patch" in line or "***Begin Patch" in line:
            start_idx = i
        elif "*** End Patch" in line or "***End Patch" in line:
            end_idx = i
            break

    if start_idx is None:
        start_idx = -1
    if end_idx is None:
        end_idx = len(lines)

    i = start_idx + 1
    current_op: PatchOperation | None = None
    current_hunk: Hunk | None = None

    while i < end_idx:
        line = lines[i]

        update_match = re.match(r"\*\*\*\s*Update\s+File:\s*(.+)", line)
        add_match = re.match(r"\*\*\*\s*Add\s+File:\s*(.+)", line)
        delete_match = re.match(r"\*\*\*\s*Delete\s+File:\s*(.+)", line)
        move_match = re.match(r"\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)", line)

        if update_match:
            if current_op is not None:
                if current_hunk is not None and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.UPDATE,
                file_path=update_match.group(1).strip(),
            )
            current_hunk = None

        elif add_match:
            if current_op is not None:
                if current_hunk is not None and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.ADD,
                file_path=add_match.group(1).strip(),
            )
            current_hunk = Hunk()

        elif delete_match:
            if current_op is not None:
                if current_hunk is not None and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.DELETE,
                file_path=delete_match.group(1).strip(),
            )
            operations.append(current_op)
            current_op = None
            current_hunk = None

        elif move_match:
            if current_op is not None:
                if current_hunk is not None and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.MOVE,
                file_path=move_match.group(1).strip(),
                new_path=move_match.group(2).strip(),
            )
            operations.append(current_op)
            current_op = None
            current_hunk = None

        elif re.match(r"\*\*\*\s*Move\s+File:", line):
            # Move 缺目标路径（无 "-> dst"）：仍进入 MOVE 解析并置 new_path=None，
            # 交由下方 parse_errors 分支给出精确的 "missing destination path" 错误，
            # 避免被当作普通文本行忽略而降级为 empty_patch。
            if current_op is not None:
                if current_hunk is not None and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)
            src = line.split("Move File:", 1)[1].strip()
            current_op = PatchOperation(
                operation=OperationType.MOVE,
                file_path=src,
                new_path=None,
            )
            operations.append(current_op)
            current_op = None
            current_hunk = None

        elif line.startswith("@@"):
            if current_op is not None:
                if current_hunk is not None and current_hunk.lines:
                    current_op.hunks.append(current_hunk)

                hint_match = re.match(r"@@\s*(.+?)\s*@@", line)
                hint = hint_match.group(1) if hint_match else None
                current_hunk = Hunk(context_hint=hint)

        elif current_op is not None and line:
            if current_hunk is None:
                current_hunk = Hunk()

            if line.startswith("+"):
                current_hunk.lines.append(HunkLine("+", line[1:]))
            elif line.startswith("-"):
                current_hunk.lines.append(HunkLine("-", line[1:]))
            elif line.startswith(" "):
                current_hunk.lines.append(HunkLine(" ", line[1:]))
            elif line.startswith("\\"):
                pass
            else:
                current_hunk.lines.append(HunkLine(" ", line))

        i += 1

    if current_op is not None:
        if current_hunk is not None and current_hunk.lines:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)

    if not operations:
        return operations, None

    parse_errors: list[str] = []
    for op in operations:
        if not op.file_path:
            parse_errors.append("Operation with empty file path")
        if op.operation == OperationType.UPDATE and not op.hunks:
            parse_errors.append(f"UPDATE {op.file_path!r}: no hunks found")
        if op.operation == OperationType.MOVE and not op.new_path:
            parse_errors.append(
                f"MOVE {op.file_path!r}: missing destination path (expected 'src -> dst')"
            )

    if parse_errors:
        return [], "Parse error: " + "; ".join(parse_errors)

    return operations, None

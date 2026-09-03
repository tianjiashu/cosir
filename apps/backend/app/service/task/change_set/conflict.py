"""撤销前磁盘状态三方态判定（R6 软冲突防护，解法 A）。

单一职责：判定「当前磁盘」相对某个反向操作处于哪种状态——已还原（before）、
Agent 改完态（after）、还是用户手动改过（两者都不是）。只读磁盘、不写文件。

设计边界：
- 与 ``operations.revert_file`` 协作：判定结果由调用方决定跳过 apply / 正常 apply /
  抛 ``PatchApplyError`` 拒绝。
- hunk 内容拼接复用 ``patch_parser.hunk_content``，不重复实现。
"""

from pathlib import Path

from app.core.tools.tool_handler.patch.patch_parser import OperationType, PatchOperation, hunk_content
from app.core.tools.tool_handler.security.path_resolver import PathResolver


def _is_already_reverted(operation: PatchOperation, resolver: PathResolver) -> bool:
    """判断单个反向操作是否已在上一次（被中断的）回退中完成，可安全跳过。

    参数:
        operation: 反向 PatchOperation（ADD/DELETE/UPDATE/MOVE）。
        resolver: workspace 路径解析器。

    返回:
        True 表示该操作目标态已达成（重入时应跳过）；False 表示仍需 apply。

    异常:
        无。磁盘读取失败（OSError）时内部捕获并返回 False，不向上抛。

    副作用:
        无。只读磁盘，不写文件。
    """
    resolved, err = resolver.resolve_within_workspace(operation.file_path)
    if resolved is None or err:
        return False
    if operation.operation == OperationType.ADD:
        if not Path(resolved).exists():
            return False
        expected = (
            operation.content
            if operation.content is not None
            else hunk_content(operation.hunks, "+")
        )
        try:
            # 用 utf-8-sig 读取以剥离磁盘字节中的 BOM 前缀；同时对 ``expected``
            # 剥离文本首字符 \ufeff（delete 采集的 before 以 utf-8 解码保留了该
            # 字符，而 apply 重建文件写入的正是 \ufeff），使两侧可比。否则含 BOM
            # 文件重入时会因 ``current`` 无 \ufeff 而判定未还原，导致重复 apply
            # 触发 destination-already-exists 卡死。
            current = Path(resolved).read_bytes().decode("utf-8-sig")
            return current == _strip_leading_bom(expected)
        except OSError:
            return False
    elif operation.operation == OperationType.DELETE:
        return not Path(resolved).exists()
    elif operation.operation == OperationType.UPDATE:
        if operation.content is None or not Path(resolved).exists():
            return False
        try:
            current = Path(resolved).read_bytes().decode("utf-8-sig")
            return current == _strip_leading_bom(operation.content)
        except OSError:
            return False
    else:  # OperationType.MOVE：文件应已移到 new_path 且源不存在
        dst, dst_err = resolver.resolve_within_workspace(operation.new_path or "")
        if dst is None or dst_err or not Path(dst).exists():
            return False
        return not Path(resolved).exists()


def _is_at_after_state(operation: PatchOperation, resolver: PathResolver) -> bool:
    """判断磁盘当前是否处于「Agent 改完态（after）」，即反向操作本应撤销掉的内容。

    R6 软冲突防护（解法 A）的对称判定：与 ``_is_already_reverted`` 互补——
    前者判定「已还原到 before」，本函数判定「仍是 Agent 改完的 after」。
    只有当两者都为 False 时，才说明用户手动改过文件（既非 before 也非 after）。

    反向操作语义映射（op_json 存的是**反向** PatchOperation，故 after 态与
    ``_is_already_reverted`` 判定的 before 态互为镜像，不可照搬其分支）：
    - 反向 ADD（原操作为 DELETE：Agent 删掉了文件，撤销需重建）：
      after = 文件**不存在**（Agent 删完的状态）。
    - 反向 DELETE（原操作为 ADD：Agent 新建了文件，撤销需删除）：
      after = 文件**存在**（Agent 建完的状态）。内容不做比对：新建后再被后续工具
      修改仍属 Agent 改动范畴，此处只防「用户手动删除/重建」造成的误覆盖。
    - 反向 UPDATE（整文件覆盖回 before）：after = 文件内容等于反向 op 的 ``+`` 行
      拼出的内容（Agent 改完态）。``v4a_reverse`` 构造反向 UPDATE 时原样保留
      正向 hunks（``-`` 行 = before、``+`` 行 = after），故 after 内容取自
      ``+`` 行，与 ``operation.content``（before 态）无关。
    - 反向 MOVE：after = 已移到 new_path 且源不存在。

    参数:
        operation: 反向 PatchOperation（ADD/DELETE/UPDATE/MOVE）。
        resolver: workspace 路径解析器。

    返回:
        True 表示磁盘处于 Agent 改完态（应正常 apply 撤销）；False 表示不是。

    异常:
        无。磁盘读取失败（OSError）时内部捕获并返回 False，不向上抛。

    副作用:
        无。只读磁盘，不写文件。
    """
    resolved, err = resolver.resolve_within_workspace(operation.file_path)
    if resolved is None or err:
        return False
    if operation.operation == OperationType.ADD:
        # 反向 ADD 对应原 DELETE：Agent 改完态就是「文件已被删除」。
        return not Path(resolved).exists()
    elif operation.operation == OperationType.DELETE:
        # 反向 DELETE 对应原 ADD：Agent 改完态就是「文件已存在」。
        return Path(resolved).exists()
    elif operation.operation == OperationType.UPDATE:
        # 注意：after 态只由反向 op 的 ``+`` 行还原，与 ``operation.content``（before 态）
        # 无关，故此处不得用 content is None 提前返回，否则无 content 的 UPDATE 反向 op
        # 会被误判为冲突。
        if not Path(resolved).exists():
            return False
        after_content = hunk_content(operation.hunks, "+")
        try:
            current = Path(resolved).read_bytes().decode("utf-8-sig")
        except OSError:
            return False
        # ``+`` 行是按行拼接的，结构上无法还原原文件的尾换行；直接全等比较会把
        # "a\nb\n" 误判成与 "a\nb" 不同，导致几乎所有正常撤销都被拒。故在尾换行
        # 维度做归一化比较。
        # 已知取舍：用户「只增删尾部空行」这一种改动不会被 R6 判定为冲突，撤销会
        # 连同该尾换行一起还原。相较「正常撤销大面积失效」，此代价可接受。
        # after_content 为空（``v4a_reverse`` 整文件清空场景的 ``+`` 行集合为空）时，
        # 归一化比较退化为「磁盘内容是否为空」：磁盘为空文件 → after 态放行撤销
        # （还原为 before）；磁盘非空（用户手改或采集缺漏）→ 拒绝。避免清空操作
        # 无法撤销，同时保持「磁盘非空时保守拒绝」的既有行为。
        return _strip_leading_bom(current).rstrip("\r\n") == _strip_leading_bom(
            after_content
        ).rstrip("\r\n")
    else:  # OperationType.MOVE：目标已移到 new_path 且源不存在
        dst, dst_err = resolver.resolve_within_workspace(operation.new_path or "")
        if dst is None or dst_err or not Path(dst).exists():
            return False
        return not Path(resolved).exists()


def _strip_leading_bom(content: str) -> str:
    """剥离文本开头的单个 U+FEFF BOM 标记，使与 ``utf-8-sig`` 解码结果可比。

    参数:
        content: 待比较的文本内容。

    返回:
        去掉开头单个 ``\\ufeff`` 后的文本；无 BOM 标记时原样返回。

    异常:
        无。

    副作用:
        无。
    """
    return content[1:] if content.startswith("\ufeff") else content

"""统一限制 ToolObservation 正文并在 workspace 内保存超大输出。

截断策略：模型可见 ``content`` 受 ``Constant.Tools.MAX_OUTPUT_CHARS`` 约束。超限时保留
**头尾两端**、中间以一行截断标记替代，而不是只留开头——终端日志、测试结果等工具输出的
结论与报错通常落在末尾，只留开头会让模型看不到关键信息。标记同时给出省略的字符数/行数
以及承载完整原文的 artifact 路径，模型可据此按需读取全文。

预算口径：``头 + 标记 + 尾`` 的总长度不超过 ``max_chars``；头尾按 ``_HEAD_BUDGET_RATIO``
分配，切口回退/前推到整行边界，避免留下半行。标记长度取决于省略量、省略量又取决于预算，
为避免循环依赖，先按「最大可能位数」渲染标记取保守上界，再据此分配头尾预算；预算连标记都
装不下时退化为「只保留裁到预算内的标记」。

边界：截断只发生在模型通道。落盘 artifact 与 ``display_data``（客户端展示）都保持完整原文，
见 ``ToolObservationBudget`` 与 ``tool_ui_display_contract.md``。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.tool_handler.patch_write.atomic_write import atomic_write_text
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.utils.cosir_paths import workspace_tool_artifact_dir

# 头尾预算分配：头部占比，其余给尾部。工具输出的结论/报错通常落在末尾，尾部必须保留；
# 该常量是头尾配比的唯一调节点。
_HEAD_BUDGET_RATIO = 0.5


def _render_truncation_marker(
    total_chars: int,
    omitted_chars: int,
    omitted_lines: int,
    artifact_path: str,
) -> str:
    """渲染夹在头尾之间的截断标记（纯函数，零 I/O）。

    参数:
        total_chars: 截断前正文的总字符数。
        omitted_chars: 被省略的字符数。
        omitted_lines: 被省略的行数（按 ``\\n`` 计）。
        artifact_path: 完整原文的 workspace 相对路径；为空表示未落盘。

    返回:
        形如 ``"... [output truncated: omitted 3 of 5000 characters (7 lines); full
        output: <path>] ..."`` 的标记文本，首尾各带空行以便与正文分离。

    异常:
        无。

    副作用:
        无。
    """

    location = f"full output: {artifact_path}" if artifact_path else "full output unavailable"
    return (
        f"\n\n... [output truncated: omitted {omitted_chars} of {total_chars} characters "
        f"({omitted_lines} lines); {location}] ...\n\n"
    )


def _snap_head_to_line_end(head: str) -> str:
    """把头部末端回退到最后一个换行之后，避免头部停在半行。"""

    cut = head.rfind("\n")
    return head[: cut + 1] if cut >= 0 else head


def _snap_tail_to_line_start(tail: str) -> str:
    """把尾部起点前推到第一个换行之后，避免尾部从半行开始。

    仅当换行之后还有内容时才前推：预算小于末行长度时，尾切片里唯一的换行就在末尾，
    此时前推会把尾部清空，反而不如保留这段半行。
    """

    cut = tail.find("\n")
    if 0 <= cut < len(tail) - 1:
        return tail[cut + 1 :]
    return tail


class ToolOutputBudget:
    """统一约束模型可见工具输出，并为超限内容保存 workspace artifact。"""

    def __init__(self, max_chars: int = Constant.Tools.MAX_OUTPUT_CHARS) -> None:
        """初始化输出预算。

        参数:
            max_chars: 模型可见 ``content`` 的最大字符数；缺省取
                ``Constant.Tools.MAX_OUTPUT_CHARS``（与工具系统装配时的取值同源）。

        返回:
            无。

        异常:
            ValueError: ``max_chars`` 小于 1 时抛出。

        副作用:
            无。
        """

        if max_chars < 1:
            raise ValueError("max_chars must be greater than zero")
        self._max_chars = max_chars

    def apply(
        self,
        observation: ToolObservation,
        execution_context: ToolExecutionContext | None,
    ) -> ToolObservation:
        """截断超大输出，并在有 workspace 时保存完整 artifact。

        参数:
            observation: handler 返回的原始观察。
            execution_context: 当前 task/workspace 上下文。

        返回:
            未超限时返回原对象；超限时返回「头 + 截断标记 + 尾」的新对象，正文长度不超过
            ``max_chars``，并在 ``artifact_data`` 中带截断标记与 artifact 路径。

        异常:
            无。artifact 写入失败仅记日志并退化为纯截断。

        副作用:
            超限且有 workspace 时在 ``<workspace_root>/.cosir/tool-artifacts`` 写入完整输出。
        """

        content = observation.content or ""
        normalized_observation = (
            observation
            if observation.content is not None
            else replace(observation, content=content)
        )

        # 未超限：原样返回，不加标记、不做复制。
        if len(content) <= self._max_chars:
            return normalized_observation

        # 超限：先把完整原文落盘，再把模型可见正文截断为「头 + 标记 + 尾」。
        artifact_path = self._write_artifact(normalized_observation, execution_context)
        artifact_data = dict(normalized_observation.artifact_data or {})
        artifact_data.update(
            {
                "output_truncated": True,
                "original_chars": len(content),
                "artifact_path": artifact_path,
            }
        )
        return replace(
            normalized_observation,
            content=self._truncate_content(content, artifact_path),
            artifact_data=artifact_data,
        )

    def _truncate_content(self, content: str, artifact_path: str) -> str:
        """把超限正文截断为「头 + 标记 + 尾」。

        参数:
            content: 截断前的完整正文。
            artifact_path: 完整原文的 artifact 路径，供标记引用；为空时标记说明未落盘。

        返回:
            保留头尾两端、中间带截断标记的正文；长度恒定不超过 ``self._max_chars``。

        异常:
            无。

        副作用:
            无（纯字符串处理）。
        """

        max_chars = self._max_chars
        total = len(content)
        total_lines = content.count("\n")
        digits = len(str(total))
        # 标记里同时含省略字符数与省略行数，二者都不超过 total；用最大位数占位渲染出标记
        # 长度的保守上界，避免「标记长度 ↔ 省略量」的循环依赖。
        marker_upper_bound = _render_truncation_marker(
            total, 10**digits - 1, 10**digits - 1, artifact_path
        )
        if max_chars <= len(marker_upper_bound):
            # 预算连标记都装不下：退化为只保留标记本身（裁到预算内），不保留正文。
            return _render_truncation_marker(total, total, total_lines, artifact_path)[:max_chars]

        budget = max_chars - len(marker_upper_bound)
        head_len = min(budget, max(0, int(budget * _HEAD_BUDGET_RATIO)))
        tail_len = budget - head_len
        head = _snap_head_to_line_end(content[:head_len])
        tail = _snap_tail_to_line_start(content[total - tail_len :]) if tail_len else ""
        marker = _render_truncation_marker(
            total,
            total - len(head) - len(tail),
            total_lines - head.count("\n") - tail.count("\n"),
            artifact_path,
        )
        return head + marker + tail

    def _write_artifact(
        self,
        observation: ToolObservation,
        execution_context: ToolExecutionContext | None,
    ) -> str:
        """把完整工具输出落盘为 workspace 内的 artifact 文件，返回相对路径。

        本函数是工具输出预算策略的落盘环节：调用方在把 ``observation.content``
        截断到预算上限前，先用本方法把**完整内容**存成文件，从而内存中只保留
        摘要、磁盘上保留全文，上层可凭返回的路径按需取回完整输出。

        参数:
            observation: 待保存的原始观察，使用其 ``content`` 作为写入内容、
                ``tool_name`` 仅用于日志诊断。
            execution_context: 当前 task/workspace 上下文，提供 ``workspace_root``
                作为 artifact 的写入基准与边界；为 ``None`` 时直接返回空串、不落盘。

        返回:
            相对 workspace 根、POSIX 风格（``/`` 分隔）的 artifact 路径；当上下文
            缺失、路径解析失败或写入异常时返回空字符串，主流程降级为"不落盘"。

        异常:
            不向上抛出。路径解析与写入阶段捕获 ``OSError`` / ``RuntimeError`` /
            ``ValueError``，记日志后返回空串。

        副作用:
            在 ``{workspace_root}/.cosir/tool-artifacts/`` 下以 UUID 命名原子
            写入文本文件（先写临时文件再 rename），工具内容保持原文。落盘路径受 ``PathResolver`` 与
            ``containment_root`` 双重约束，不会写到 workspace 之外；``.cosir`` 只读保留区对本
            内部子系统显式豁免；失败时记录异常/错误日志。
        """

        if execution_context is None:
            return ""
        root = Path(execution_context.workspace_root).resolve()
        target = workspace_tool_artifact_dir(root) / f"{uuid4().hex}.txt"
        resolver = PathResolver(root)
        try:
            # 工具输出 artifact 属内部子系统，显式豁免 `.cosir` 只读保留区。
            resolved, error = resolver.resolve_within_workspace(str(target), allow_reserved=True)
        except (OSError, RuntimeError, ValueError):
            log.exception(
                "tool_artifact_path_resolution_failed",
                extra={
                    "msg": "工具输出 artifact 路径解析异常",
                    "data": {"tool_name": observation.tool_name},
                },
            )
            return ""
        if resolved is None:
            log.error(
                "tool_artifact_path_invalid",
                extra={
                    "msg": "工具输出 artifact 路径解析失败",
                    "data": {"error": error, "tool_name": observation.tool_name},
                },
            )
            return ""
        try:
            atomic_write_text(
                resolved,
                observation.content or "",
                preserve_eol=False,
                containment_root=root,
            )
        except (OSError, RuntimeError, ValueError):
            log.exception(
                "tool_artifact_write_failed",
                extra={
                    "msg": "工具输出 artifact 写入失败",
                    "data": {
                        "tool_name": observation.tool_name,
                        "path": str(target),
                    },
                },
            )
            return ""
        return resolved.relative_to(root).as_posix()

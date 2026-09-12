"""统一限制 ToolObservation 正文并在 workspace 内保存超大输出。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from app.config.logging.logger import log
from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.tool_handler.file_io.atomic_write import atomic_write_text
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.utils.trace_infra.redaction import redact_terminal_output


class ToolOutputBudget:
    """对所有工具观察应用统一字符预算和 artifact spill。"""

    def __init__(self, max_chars: int = 20_000) -> None:
        """初始化输出预算。

        参数:
            max_chars: 模型可见 ``content`` 的最大字符数。

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
            未超限时返回原对象；超限时返回带截断标记和 artifact 元数据的新对象。

        异常:
            无。artifact 写入失败仅记日志并退化为纯截断。

        副作用:
            超限且有 workspace 时在 ``.coding-agent/tool-artifacts`` 写入完整输出。
        """

        # 对终端输出进行脱敏；ToolObservation 允许没有正文的失败/取消观察。
        content = observation.content or ""
        redacted_content = redact_terminal_output(content)
        safe_observation = (
            observation
            if redacted_content == content and observation.content is not None
            else replace(observation, content=redacted_content)
        )

        # 如果内容长度小于预算，则直接返回
        if len(redacted_content) <= self._max_chars:
            return safe_observation

        # 如果内容长度大于预算，则写入 artifact 文件，并返回带 artifact 路径的截断内容
        artifact_path = self._write_artifact(safe_observation, execution_context)
        if artifact_path:
            hint = f"\n... [output truncated; full output: {artifact_path}]"
        else:
            hint = "\n... [output truncated by global tool budget]"
        visible_hint = hint[: self._max_chars]
        visible_prefix = redacted_content[: max(0, self._max_chars - len(visible_hint))]
        artifact_data = dict(safe_observation.artifact_data or {})
        artifact_data.update(
            {
                "output_truncated": True,
                "original_chars": len(content),
                "artifact_path": artifact_path,
            }
        )
        return replace(
            safe_observation,
            content=visible_prefix + visible_hint,
            artifact_data=artifact_data,
        )

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
            缺失、路径解析失败或写入异常时返回空字符串，主流程降级为“不落盘”。

        异常:
            不向上抛出。路径解析与写入阶段捕获 ``OSError`` / ``RuntimeError`` /
            ``ValueError``，记日志后返回空串。

        副作用:
            在 ``{workspace_root}/.coding-agent/tool-artifacts/`` 下以 UUID 命名原子
            写入文本文件（先写临时文件再 rename）；写入前对内容做终端输出脱敏；
            落盘路径受 ``ProjectPathResolver`` 与 ``containment_root`` 双重约束，
            不会写到 workspace 之外；失败时记录异常/错误日志。
        """

        if execution_context is None:
            return ""
        relative = Path(".coding-agent") / "tool-artifacts" / f"{uuid4().hex}.txt"
        resolver = PathResolver(execution_context.workspace_root)
        try:
            resolved, error = resolver.resolve_within_workspace(str(relative))
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
                redact_terminal_output(observation.content or ""),
                preserve_eol=False,
                containment_root=execution_context.workspace_root,
            )
        except (OSError, RuntimeError, ValueError):
            log.exception(
                "tool_artifact_write_failed",
                extra={
                    "msg": "工具输出 artifact 写入失败",
                    "data": {
                        "tool_name": observation.tool_name,
                        "path": str(relative),
                    },
                },
            )
            return ""
        return relative.as_posix()

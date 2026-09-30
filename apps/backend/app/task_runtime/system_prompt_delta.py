"""Task 级系统提示词运行时增量通知契约。

本模块只定义系统提示词变更在进程内传播时使用的值对象与 LangChain 消息映射，不负责读取
配置文件、计算配置作用域，也不负责把通知写入 Conversation context。通知的唯一运行时
副作用是作为模型请求的临时 system message 使用；持久化由后续的 context 方案负责。
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import SystemMessage

from app.task_runtime.system_prompt_delta_source import SystemPromptDeltaSource

__all__ = ["SystemPromptDelta", "SystemPromptDeltaSource", "SYSTEM_PROMPT_DELTA_NOTICE_TYPE"]

SYSTEM_PROMPT_DELTA_NOTICE_TYPE = "system_prompt_delta"


@dataclass(frozen=True)
class SystemPromptDelta:
    """描述一条只在当前 backend 进程内生效的系统提示词增量通知。

    参数:
        source: 发生变化的系统提示词层。
        diff: 只包含新增、删除和修改行的 unified diff，不包含完整新提示词。
        scope: 可选的作用域标识，例如 workspace 根目录；仅用于广播过滤和诊断。

    返回:
        :meth:`to_message` 返回供模型请求使用的临时 ``SystemMessage``。

    异常:
        无。调用方应在构造前保证 ``diff`` 非空且来源已通过配置校验。

    副作用:
        无。本对象不会写入数据库、context 或 Transport snapshot。
    """

    source: SystemPromptDeltaSource
    diff: str
    scope: str | None = None

    def to_message(self) -> SystemMessage:
        """把增量通知转换为模型可见的临时系统消息。

        返回:
            包含简短覆盖规则与 diff 的 ``SystemMessage``。消息通过
            ``additional_kwargs.notice_type`` 与普通工具修复提示区分。

        异常:
            ValueError: ``diff`` 为空白，表示没有实际配置变化。

        副作用:
            无；返回值由调用方决定是否加入单次模型请求。
        """

        if not self.diff.strip():
            raise ValueError("system prompt delta must not be blank")
        scope_line = f"\n作用域：{self.scope}" if self.scope else ""
        content = (
            "<system_prompt_update>\n"
            "系统提示词已变更，请立即应用以下增量：\n"
            "- 未出现在 diff 中的规则保持不变；\n"
            "- 被删除的规则不再生效；\n"
            "- 新规则和修改后的规则优先于旧规则。\n"
            f"来源：{self.source.value}{scope_line}\n"
            "<diff>\n"
            f"{self.diff.rstrip()}\n"
            "</diff>\n"
            "</system_prompt_update>"
        )
        return SystemMessage(
            content=content,
            additional_kwargs={
                "notice_type": SYSTEM_PROMPT_DELTA_NOTICE_TYPE,
                "source": self.source.value,
                "scope": self.scope,
            },
        )

"""Task 级系统提示词运行时增量通知契约。

本模块只定义系统提示词变更在进程内传播时使用的值对象与运行时通知映射，不负责读取
配置文件、计算配置作用域，也不负责把通知写入 Conversation context。
通知进入 Task 队列后由模型节点转换为消息并写入 context。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.runtime_notice import RuntimeNotice
from app.task_runtime.system_prompt_delta_source import SystemPromptDeltaSource

__all__ = ["SYSTEM_PROMPT_DELTA_NOTICE_TYPE", "SystemPromptDelta", "SystemPromptDeltaSource"]

SYSTEM_PROMPT_DELTA_NOTICE_TYPE = "system_prompt_delta"


@dataclass(frozen=True)
class SystemPromptDelta:
    """描述一条只在当前 backend 进程内生效的系统提示词增量通知。

    参数:
        source: 发生变化的系统提示词层。
        diff: 只包含新增、删除和修改行的 unified diff，不包含完整新提示词。
        scope: 可选的作用域标识，例如 workspace 根目录；仅用于广播过滤和诊断。

    返回:
        :meth:`to_notice` 返回供运行时队列使用的系统通知 ``RuntimeNotice``。

    异常:
        无。调用方应在构造前保证 ``diff`` 非空且来源已通过配置校验。

    副作用:
        无。本对象不会写入数据库、context 或 Transport snapshot。
    """

    source: SystemPromptDeltaSource
    diff: str
    scope: str | None = None

    def to_notice(self) -> RuntimeNotice:
        """把增量转换为不绑定模型消息角色的运行时通知。

        返回:
            包含简短覆盖规则与 diff 的 ``RuntimeNotice``。通知通过
            ``kind`` 与普通工具修复提示区分。

        异常:
            ValueError: ``diff`` 为空白，表示没有实际配置变化。

        副作用:
            无；返回值交给队列，模型节点消费时才构造消息并写入 context。
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
        return RuntimeNotice(
            content=content,
            kind=SYSTEM_PROMPT_DELTA_NOTICE_TYPE,
            metadata={
                "source": self.source.value,
                "scope": self.scope,
            },
        )

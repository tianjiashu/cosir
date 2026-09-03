"""系统提示词构建所需的运行时事实。"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from platform import system

from app.config.settings import Settings


def _default_coding_rule_dir() -> str:
    """返回默认编码规则的绝对路径。"""
    return str(Path(__file__).resolve().parent / "rules" / "default-coding-rules.md")


@dataclass(frozen=True)
class SystemPromptContext:
    """承载构建系统提示词时需要注入的运行时事实。

    参数:
        os_name: 当前运行操作系统名称。
        workspace_root: 当前工作区根路径。
        today: 当前本地日期。

    返回:
        不可变的系统提示词上下文值对象。

    异常:
        无。

    副作用:
        无。
    """

    os_name: str
    workspace_root: str
    today: str
    language: str = Settings.DEFAULT_LANGUAGE
    coding_rule_dir: str = field(default_factory=_default_coding_rule_dir)

    @classmethod
    def build(
        cls,
        workspace_root: str | None = None,
    ) -> "SystemPromptContext":
        """从当前本地环境创建系统提示词上下文。

        参数:
            workspace_root: 当前工作区根路径；为空时使用 ``unknown``。

        返回:
            可交给 ``SystemPromptBuilder`` 消费的上下文值对象。

        异常:
            无。

        副作用:
            无。
        """

        return cls(
            os_name=system() or "unknown",
            workspace_root=workspace_root or "unknown",
            today=date.today().isoformat(),
        )

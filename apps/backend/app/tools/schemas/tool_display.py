"""工具展示提示值对象（ToolDisplayHints）。

本模块承载「工具在前端应如何展示」的语义元数据，是 ``ToolDefinition``
契约的一部分，也是不同工具展示差异收敛到后端的唯一事实来源。前端只保留
一个通用渲染引擎，按本对象投影出的字段做模板渲染，不按工具名写特化分支。

设计边界：
- 零依赖（不 import ``app.*`` 之外的业务模块），避免循环依赖与包初始化污染。
- 渲染逻辑（从 ``arguments`` 派生展示字段）收进值对象自身的 ``render`` 方法，
  符合「值对象自带投影方法」的项目约定。
- ``summary_template`` 使用 ``str.format`` 占位符，占位名来自工具参数字典；
  分页类工具会自动派生 ``start`` / ``end`` 行号，便于渲染 ``L{start}-L{end}``。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolDisplayHints:
    """工具在前端如何展示的契约元数据。

    一个工具「应该强调哪些参数、如何概括为一句话、点击后做什么」都收敛到这里，
    而不是散落到前端各组件。所有字段都是声明式的，便于工具作者一次性定义。

    参数:
        verb: 动作名，如 “读取”、“搜索”，前端作为主标题动词。
        icon: lucide 图标名，如 “eye”、“search”，前端据此渲染图标。
        summary_template: 摘要模板，使用 ``str.format`` 占位符引用参数字典；
            为 ``None`` 时降级为 ``verb + 主参数``。
        detail_keys: 展开态优先展示的参数 key 顺序（其余参数按字典序兜底）。
        click_action: 可选点击动作模板，格式 ``"<action>:<target_template>"``，
            例如 ``"open_file:{path}"``；渲染时解析为结构化
            ``{"action": ..., "target": ...}``，交由前端按 action 分发。

    返回:
        无。

    异常:
        无。

    副作用:
        无（frozen dataclass，不可变）。
    """

    verb: str
    icon: str
    summary_template: str | None = None
    detail_keys: tuple[str, ...] = field(default_factory=tuple)
    click_action: str | None = None

    def render(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """把一次工具调用参数投影成前端可读的展示字典。

        参数:
            arguments: 本次工具调用的实际参数字典。

        返回:
            含 ``verb`` / ``icon`` / ``summary`` / ``detail_keys`` / ``click_action``
            的字典，可直接序列化进事件 payload 由前端通用渲染。

        异常:
            不向上抛出；模板缺字段或其他渲染异常时降级为 ``verb + 主参数``。

        副作用:
            无。
        """

        # 派生一份可渲染字典：分页类工具自动补 start/end 行号。
        # 始终派生（使用默认值），确保摘要模板始终能渲染出行号范围，
        # 避免 LLM 不传 offset/limit 时模板 KeyError 降级为无行号摘要。
        derived: dict[str, Any] = dict(arguments)
        _has_pagination = "offset" in arguments or "limit" in arguments
        start = int(arguments.get("offset", 1))
        limit = int(arguments.get("limit", 500))
        derived["start"] = start
        derived["end"] = start + limit - 1 if limit > 0 else start

        # 路径类工具自动派生文件名（basename），供摘要精简展示，
        # 而 click_action 仍可引用完整 {path} 用于打开文件等动作。
        if "path" in arguments and isinstance(arguments["path"], str):
            derived["path_basename"] = Path(arguments["path"]).name

        summary = self._render_summary(derived)

        click_action = self._render_click_action(derived)

        return {
            "verb": self.verb,
            "icon": self.icon,
            "summary": summary,
            "detail_keys": list(self.detail_keys),
            "click_action": click_action,
        }

    def _render_summary(self, derived: dict[str, Any]) -> str:
        """根据模板或降级策略计算摘要文本。

        参数:
            derived: 已合并派生字段（含 start/end）的参数字典。

        返回:
            摘要文本；模板渲染失败时降级为 ``verb + 主参数``。

        异常:
            不向上抛出。

        副作用:
            无。
        """

        if self.summary_template:
            try:
                return self.summary_template.format(**derived)
            except (KeyError, IndexError, ValueError):
                # 模板引用了不存在的字段：降级，避免展示异常模板文本。
                pass
        head = next(iter(self.detail_keys), None)
        return f"{self.verb} {derived.get(head)}" if head else self.verb

    def _render_click_action(self, derived: dict[str, Any]) -> dict[str, str] | None:
        """把 click_action 模板解析为前端可消费的结构化动作。

        参数:
            derived: 已合并派生字段的参数字典。

        返回:
            ``{"action": ..., "target": ...}``；无 click_action 或解析失败时返回 ``None``。

        异常:
            不向上抛出。

        副作用:
            无。
        """

        if not self.click_action:
            return None
        action, sep, target_tpl = self.click_action.partition(":")
        if not sep:
            # 缺少 ":" 分隔，模板非法，安全降级。
            return None
        try:
            target = target_tpl.format(**derived)
        except (KeyError, IndexError, ValueError):
            target = ""
        return {"action": action, "target": target}

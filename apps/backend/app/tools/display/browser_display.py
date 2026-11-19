"""内置浏览器工具的 UI 展示数据构造（纯函数，不执行 IO）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# 每一步目标说明的长度上限：来自网页文字，卡片上一行放得下即可。
_TARGET_MAX_CHARS = 80


def build_browser_page_display_data(
    page: dict[str, Any],
    *,
    screenshot: str | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """浏览器工具的页面卡片：标签、地址与标题，可带截图（附件 asset id）或动作步骤。

    快照正文只给模型，不进展示数据（体积大，且 UI 用不上）；卡片据 ``tab_id`` 在浏览器面板
    打开实时页面。
    """

    data: dict[str, Any] = {
        "kind": "browser-page",
        "tab_id": page["tab_id"],
        "url": page["url"],
        "title": page["title"],
    }
    if screenshot is not None:
        data["screenshot"] = screenshot
    if steps is not None:
        data["steps"] = steps
    return data


def build_act_steps(
    actions: list[dict[str, Any]], outcome: dict[str, Any], hint: Callable[[str], str]
) -> list[dict[str, Any]]:
    """``browser_act`` 的每一步：动作类型、目标说明（宿主给的元素角色与名称、坐标）与结局。

    结局原样取宿主报的每一步状态，不从参数推断：``done`` / ``failed``（带短提示 ``hint``）/ ``unknown``
    （可能已生效、须先看页面）/ ``skipped``（停下之后没执行）。目标说明来自网页文字，只用于显示，截到一行。
    """

    steps: list[dict[str, Any]] = []
    for action, result in zip(actions, outcome["execution"]["steps"], strict=True):
        step: dict[str, Any] = {
            "action": action["type"],
            "target": _clip(result["target"]),
            "status": result["status"],
        }
        if result["error"] is not None:
            step["hint"] = hint(result["error"]["code"])
        steps.append(step)
    return steps


def _clip(text: str | None) -> str | None:
    if text is None or len(text) <= _TARGET_MAX_CHARS:
        return text
    return f"{text[:_TARGET_MAX_CHARS]}…"

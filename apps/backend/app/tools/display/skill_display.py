"""``skill`` 工具的 UI 展示数据构造。"""

from typing import Any



def build_skill_list_display_data(skills: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "skill-content", "action": "list", "skills": skills}


def build_skill_content_display_data(*, skill_name: str, resource_path: str) -> dict[str, Any]:
    return {
        "kind": "skill-content",
        "action": "load",
        "skill_name": skill_name,
        "resource_path": resource_path,
    }

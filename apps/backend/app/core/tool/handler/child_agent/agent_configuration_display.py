"""子 Agent 配置提案的 UI 展示数据构造。"""

from typing import Any


def build_agent_configuration_draft_display_data(
    *,
    agent_id: str,
    role: str,
    description: str,
    system_prompt: str,
) -> dict[str, Any]:
    """构造只读对话卡片和 Workbench 所需的最小配置草稿。

    返回值只包含 Agent 需要生成的四个字段，不包含保存结果、文件路径、版本或兼容性字段；
    前端必须把它视为临时工作副本，不能据此推断配置已写入。
    """

    return {
        "kind": "agent-configuration-draft",
        "status": "draft",
        "agent_id": agent_id,
        "role": role,
        "description": description,
        "system_prompt": system_prompt,
    }

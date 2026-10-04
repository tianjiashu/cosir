"""内置工具的「分组标签」词表（单一事实来源）。

分组标签是装配期元数据：每个 handler 声明自己所属的分组，经 ``ToolDefinition.group`` 透传给
上层（工具清单组织、展示与配置中心消费）。取值集中在本模块的理由与
:mod:`app.core.tools.schemas.tool_names` 相同——分组标签是同一事实的多点引用，散落成各 handler
的字面量后，任何一处改写都会静默产生「同一分组两种写法」，而消费方只能靠字符串比较辨认。

本模块只定义取值集合，不定义分组语义、成员归属与消费方式：谁属于哪个分组由各 handler 自己声明
（``group = TOOL_GROUP_XXX``）。分组名与工具名的相互换算由 ``ToolRegistry``
（``tool_groups_to_tool_names`` / ``tool_names_to_tool_groups``）承担，目录聚合（分组 + 组内工具
名称与描述）由消费方按需完成；工具注册、权限判断与执行同样不在本模块职责内。
"""

from typing import Final

TOOL_GROUP_FILE_EDIT: Final[str] = "文件编辑工具"
TOOL_GROUP_SEARCH: Final[str] = "文件读取工具"
TOOL_GROUP_TERMINAL: Final[str] = "终端工具"
TOOL_GROUP_TERMINAL_SESSION: Final[str] = "交互终端工具"
TOOL_GROUP_WEB: Final[str] = "联网工具"
TOOL_GROUP_CHILD_AGENT: Final[str] = "子Agent工具"
TOOL_GROUP_CONFIGURATION: Final[str] = "配置生成工具"
TOOL_GROUP_AGENT_TEAM: Final[str] = "Agent Team 工具"

DEFAULT_TOOL_GROUP = "其他工具"

"""内置工具的「分组标签」词表（单一事实来源）。

工具分组是装配期元数据：每个 handler 声明自己所属的分组，经 ``ToolDefinition.group`` 透传给
上层（工具清单组织、展示与后续消费）。取值集中在本模块的理由与 :mod:`tool_names` 相同——分组
标签是同一事实的多点引用，散落成各 handler 的字面量后，任何一处改写都会静默产生「同一分组两种
写法」，而消费方只能靠字符串比较辨认。

本模块只定义取值集合，不定义分组语义、成员归属与消费方式：谁属于哪个分组由各 handler 自己声明
（``group = TOOL_GROUP_XXX``），分组的排序、展示与用途由消费方决定。
"""

from typing import Final

TOOL_GROUP_FILE_EDIT: Final[str] = "文件编辑工具"
TOOL_GROUP_SEARCH: Final[str] = "文件读取工具"
TOOL_GROUP_TERMINAL: Final[str] = "终端工具"
TOOL_GROUP_TERMINAL_SESSION: Final[str] = "交互终端工具"
TOOL_GROUP_WEB: Final[str] = "联网工具"
TOOL_GROUP_CHILD_AGENT: Final[str] = "子Agent工具"

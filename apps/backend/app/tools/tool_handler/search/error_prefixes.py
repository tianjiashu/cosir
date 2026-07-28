"""搜索引擎错误前缀契约。

本模块只承载引擎层错误文案的前缀常量：引擎以该前缀构造错误字符串，工具层以
同一常量做前缀判定分支为 ``tool_error``，双方共享单一事实来源，避免散落字面量
导致文案变更后分支静默失效。

设计边界：
- 只定义常量，不承载任何逻辑。
"""

INVALID_REGEX_PREFIX = "invalid regex:"
PATH_NOT_FOUND_PREFIX = "Path not found:"

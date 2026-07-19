"""具体工具实现包。

约定：
- 每个工具优先拥有独立实现文件，例如 ``read_file.py``。
- 工具参数模型放在 ``app.tools.tool_schemas``，不要写在 handler 文件里。
- handler 文件只负责执行逻辑、错误归一化和组装 ``ToolDefinition``。
"""

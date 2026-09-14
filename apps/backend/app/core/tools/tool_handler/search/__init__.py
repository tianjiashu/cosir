"""搜索引擎包。

本包承载纯 Python 的正则内容搜索与文件名 glob 搜索两个 engine，以及两者共享的
文件/目录 scope 与目录遍历原语。engine 只返回结构化结果，不关心工具权限或观察构造。
"""

from app.core.tools.tool_handler.search.content_engine import search_content
from app.core.tools.tool_handler.search.filename_engine import find_files
from app.core.tools.tool_handler.search.scope import SearchScope

__all__ = ["SearchScope", "find_files", "search_content"]

"""搜索引擎包。

本包承载纯 Python 的正则内容搜索与文件名 glob 搜索两个引擎，以及两者共享的
目录遍历原语与错误前缀契约。只做搜索匹配、排序与分页，不关心工具权限。
"""

from app.core.tools.tool_handler.search.content_search import search_content
from app.core.tools.tool_handler.search.filename_search import search_filenames

__all__ = ["search_content", "search_filenames"]

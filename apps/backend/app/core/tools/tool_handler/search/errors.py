"""搜索 engine 的结构化错误。"""


class SearchError(Exception):
    """搜索失败的可分类基类；不负责构造 ToolObservation。"""


class SearchPathNotFound(SearchError):
    """搜索路径不存在、不是文件或目录。"""


class SearchPathUnreadable(SearchError):
    """搜索路径存在但不可读取。"""


class InvalidSearchPattern(SearchError):
    """内容正则表达式无效。"""


class SearchTimedOut(SearchError):
    """搜索超过 handler 的线程内协作式时间预算。"""

"""变更集服务包（查询聚合 + 单文件保留/撤销）。

对外门面：把 ``query`` / ``operations`` 的公开入口与领域异常统一在此 re-export，
调用方（API 层、测试）只需 ``from app.service.task.change_set import ...``，
不需要感知包内模块划分。包内部模块按职责单一拆分，避免单个服务文件膨胀。
"""

from app.service.task.change_set.errors import ChangeSetConflictError
from app.service.task.change_set.operations import keep_file, revert_file
from app.service.task.change_set.query import query_change_set

__all__ = [
    "ChangeSetConflictError",
    "keep_file",
    "query_change_set",
    "revert_file",
]

"""变更集领域异常。"""


class ChangeSetConflictError(ValueError):
    """变更集操作因并发状态冲突而被拒绝。

    与「路径无变更」（``ValueError``，API 映射 404）区分：本异常表示资源存在但
    其处理态已被并发方改掉（CAS miss，lost update 防护），语义属并发冲突，应由
    API 层映射为 409。继承 ``ValueError`` 以保持与既有调用方（catch ValueError）
    的向后兼容。
    """

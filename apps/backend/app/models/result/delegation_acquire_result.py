"""委派并发额度 acquire 结果值对象。

单一职责：承载一次 ``DelegationService.try_create_pending`` 的 acquire 结果——
是否成功、成功时新建的 delegation 标识、失败原因。不负责额度校验或创建逻辑
（由 service 编排、CRUD 事务落地），也不持有数据库连接。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationAcquireResult:
    """一次委派并发额度 acquire 尝试的结果。

    参数:
        acquired: 是否成功取得并发额度并创建 delegation。
        delegation_id: acquire 成功时新建 delegation 的标识；失败时为空字符串。
        reason: acquire 失败原因；成功时为空字符串。唯一取值见
            ``REASON_CONCURRENCY_EXCEEDED``。

    返回:
        一个不可变的 acquire 结果值对象。

    异常:
        无。

    副作用:
        无。
    """

    acquired: bool
    delegation_id: int
    reason: str

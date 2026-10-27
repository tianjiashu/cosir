"""进程内运行时系统通知的通用广播能力。

本模块只负责遍历已经物化的 ``TaskRuntimeSpace``、按调用方提供的匹配函数筛选目标，再把
``NoticeBody`` 经 ``task_runtime_spaces.enqueue`` 写入这些 Task 的外部消息队列（持久化，正在
执行的 Task 随即投递）。不负责构造消息内容、读取配置文件或修改 Agent Registry；具体通知类型由
各自的 configuration service 构造。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from app.config.logging.logger import log
from app.core.workflow.inbox.task_message import NoticeBody, TaskMessage
from app.task_runtime.task_runtime_space import TaskRuntimeSpace
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

RuntimeNoticeMatcher = Callable[[TaskRuntimeSpace], bool]


def broadcast_runtime_notice(
        *,
        notice: NoticeBody,
        should_notify: RuntimeNoticeMatcher,
        event: str,
        log_message: str,
        data: Mapping[str, object],
) -> int:
    """将一条运行时系统通知写入符合条件的已有 Task 的外部消息队列。

    参数:
        notice: 下一次模型请求前并入的系统通知；本函数不解析或修改其内容。
        should_notify: 按单个 runtime space 判断是否应接收消息的显式匹配函数。
        event: 结构化日志事件名。
        log_message: 结构化日志的人类可读说明。
        data: 通知类型相关的诊断字段；不会记录消息正文。

    返回:
        实际入队的 Task 数量。尚未物化、没有 context manager 或未命中匹配函数的 Task 不计入结果。

    异常:
        ``should_notify`` 抛出的异常按原样传播，表示匹配契约或运行时装配出现问题；调用方
        若通知属于已提交配置的旁路，应在更外层记录并降级，而不是回滚配置事实。

    副作用:
        读取当前进程已登记的 runtime space 快照，为命中的 Task 各写入一行持久化通知（每个 Task 一笔
        事务），正在执行的 Task 随即投递；无命中 Task 时不访问调度层；不创建新的 space、不启动 Run、
        不打断正在进行的模型请求。
    """

    spaces = task_runtime_spaces.existing_spaces()
    task_ids = [space.task_id for space in spaces if should_notify(space)]
    # 只发给已物化的 Task：它们的 actor 已在监听，通知入队后随下一次推进写入。
    for task_id in task_ids:
        task_runtime_spaces.enqueue(task_id, TaskMessage.of(notice))
    delivered = len(task_ids)
    log.info(
        event,
        extra={
            "msg": log_message,
            "data": {
                **data,
                "materialized_space_count": len(spaces),
                "delivered_count": delivered,
            },
        },
    )
    return delivered

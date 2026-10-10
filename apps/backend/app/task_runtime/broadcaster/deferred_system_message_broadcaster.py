"""进程内延迟系统消息的通用广播能力。

本模块只负责遍历已经物化的 ``TaskRuntimeSpace``、按调用方提供的匹配函数筛选目标并把
``SystemMessage`` 放入各自的延迟队列。不负责构造消息内容、读取配置文件或修改 Agent
Registry；具体通知类型由各自的 configuration service 构造。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from langchain_core.messages import SystemMessage

from app.config.logging.logger import log
from app.task_runtime.task_runtime_space import TaskRuntimeSpace
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

DeferredSystemMessageMatcher = Callable[[TaskRuntimeSpace], bool]


def broadcast_deferred_system_message(
    *,
    message: SystemMessage,
    should_notify: DeferredSystemMessageMatcher,
    event: str,
    log_message: str,
    data: Mapping[str, object],
) -> int:
    """将一条延迟系统消息投递给符合条件的已有 Task runtime space。

    参数:
        message: 下一次模型节点消费的系统消息；本函数不解析或修改其内容。
        should_notify: 按单个 runtime space 判断是否应接收消息的显式匹配函数。
        event: 结构化日志事件名。
        log_message: 结构化日志的人类可读说明。
        data: 通知类型相关的诊断字段；不会记录消息正文。

    返回:
        实际入队的 runtime space 数量。尚未物化、没有 context manager 或未命中匹配函数的
        Task 不计入结果。

    异常:
        ``should_notify`` 抛出的异常按原样传播，表示匹配契约或运行时装配出现问题；调用方
        若通知属于已提交配置的旁路，应在更外层记录并降级，而不是回滚配置事实。

    副作用:
        读取当前进程已登记的 runtime space 快照，并向命中的 space 延迟队列各写入一条消息；
        不创建新的 space、不写数据库、不打断正在进行的模型请求。
    """

    spaces = task_runtime_spaces.existing_spaces()
    delivered = 0
    for space in spaces:
        if should_notify(space):
            space.defer_system_message(message)
            delivered += 1
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

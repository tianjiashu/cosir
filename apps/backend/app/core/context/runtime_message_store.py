"""运行时消息持久化端口协议（core/context → service 的依赖倒置边界）。

本模块是 ``RuntimeContextManager`` 落库的唯一依赖端口：manager 通过注入的
``RuntimeMessageStore`` 实现读、写、清空 ``turn_messages`` 表，从而避免
``core/context`` 反向依赖 ``service`` 层（分层约束见 ``AGENTS.md``）。

实现方位于 ``service/task/turn_service.TurnRuntimeMessageStore``（复用
``TurnService.load_turn_messages`` / ``append_turn_message`` / ``clear_turn_messages``）。
本文件只定义协议，不含任何 service 依赖，使 ``core/context`` 保持轻量。
"""

from __future__ import annotations

from typing import Protocol

from app.models import RuntimeMessage


class RuntimeMessageStore(Protocol):
    """运行时消息持久化端口（由 service 层实现，注入避免 core→service 反向依赖）。

    约定：``append`` 失败抛 ``sqlalchemy.exc.SQLAlchemyError``（透传给 manager 决定
    防撕裂语义）；``build_for_task`` 按 ``task_id`` 读回**跨 turn** 的有序历史
    （含 excluded_turn_ids 排除项），供 ``RuntimeContextManager`` 重建内存上下文。
    """

    def append(self, turn_id: int, message: RuntimeMessage, sequence: int) -> None:
        """落库一条消息；失败抛 SQLAlchemyError（透传给 manager 决定防撕裂语义）。

        参数:
            turn_id: 目标 turn 标识。
            message: 单条模型无关的运行时消息。
            sequence: 轮内自增序号，由 manager 维护并传入。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败时抛出。
        """
        ...

    def clear(self, turn_id: int) -> None:
        """清空某 turn 的全部消息（turn 启动重置用）。

        参数:
            turn_id: 目标 turn 标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 删除失败时抛出。
        """
        ...

    def build_for_task(
        self,
        task_id: int,
    ) -> list[RuntimeMessage]:
        """按 task 维度读回有序历史（含跨轮），供 build_for_task 重建内存上下文。

        参数:
            task_id: 目标 task 标识。
            excluded_turn_ids: 需要排除的 turn 标识元组（如 child 排除父 turn）。

        返回:
            按 turn 顺序排列的 ``RuntimeMessage`` 列表；无历史时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库读取失败时抛出。
        """
        ...

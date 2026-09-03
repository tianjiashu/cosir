"""运行时消息持久化端口协议（core/context → service 的依赖倒置边界）。

本模块是 ``RuntimeContextManager`` 落库的唯一依赖端口：manager 通过注入的
``RuntimeMessageStore`` 实现读、写、清空 ``turn_messages`` 表，从而避免
``core/context`` 反向依赖 ``service`` 层（分层约束见 ``AGENTS.md``）。

实现方位于 ``service/task/conversation_run_state_service.ConversationRunMessageStore``（复用
``ConversationRunStateService.load_turn_messages`` / ``append_turn_message`` / ``clear_turn_messages``）。
本文件只定义协议，不含任何 service 依赖，使 ``core/context`` 保持轻量。
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from app.core.context.context_entry import ContextEntry
from app.models import RuntimeMessage


class RuntimeMessageStore(Protocol):
    """运行时消息持久化端口（由 service 层实现，注入避免 core→service 反向依赖）。

    约定：``append`` 失败抛 ``sqlalchemy.exc.SQLAlchemyError``（透传给 manager 决定
    防撕裂语义）；``build_for_task`` 按 ``task_id`` 读回**跨 turn** 的有序历史
    （含 excluded_run_ids 排除项），供 ``RuntimeContextManager`` 重建内存上下文。
    """

    def append(
        self,
        run_id: int,
        message: RuntimeMessage,
        sequence: int,
        include_in_context: bool = True,
    ) -> None:
        """落库一条消息；失败抛 SQLAlchemyError（透传给 manager 决定防撕裂语义）。

        参数:
            run_id: 目标 turn 标识。
            message: 单条模型无关的运行时消息。
            sequence: 轮内自增序号，由 manager 维护并传入。
            include_in_context: 是否纳入后续模型上下文。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败时抛出。
        """
        ...

    def clear(self, run_id: int) -> None:
        """清空某 turn 的全部消息（turn 启动重置用）。

        参数:
            run_id: 目标 turn 标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 删除失败时抛出。
        """
        ...

    def build_for_task(
        self,
        task_id: int,
        excluded_run_ids: Collection[int] | None = None,
    ) -> list[ContextEntry]:
        """按 task 维度读回有序历史，支持排除当前执行 turn。

        参数:
            task_id: 目标 task 标识。
            excluded_run_ids: 需要排除的 turn 标识集合，通常用于排除当前执行 turn。

        返回:
            按 turn 顺序排列的 ``ContextEntry`` 列表；无历史时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库读取失败时抛出。
        """
        ...

    def build_for_run(self, run_id: int) -> list[ContextEntry]:
        """按 turn 读取当前 turn 的有效上下文轨迹。

        参数:
            run_id: 目标 turn 标识。

        返回:
            按 sequence 排序的上下文条目列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库读取失败时抛出。
        """
        ...

    def next_run_sequence(self, run_id: int) -> int:
        """返回指定 turn 下一条消息可用的 sequence。"""
        ...

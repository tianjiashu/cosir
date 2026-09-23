"""孤儿 LangGraph checkpoint 线程回收（基础设施层）。

单一职责：删除已不在主库中任何 run 引用的 LangGraph checkpoint 线程对应的
``writes`` / ``checkpoints`` 记录。checkpoint 库是独立的 SQLite 数据库，由
``app.core.workflows.agent_workflow.build_checkpointer`` 经 aiosqlite 直连，与本模块解耦；级联删除
提交主库之后调用本模块做失败安全的 GC，不阻断主流程。

职责边界：
- 负责：对 checkpoint sqlite 的孤儿线程做删除。
- 不负责：主库级联删除编排（见 ``app.storage.crud`` 与各 service 层）、
  任何业务规则或事件广播。
"""

import sqlite3
from pathlib import Path

from app.config.logging.logger import log
from app.storage.store_engines import checkpoint_path


def cleanup_orphan_checkpoint_threads(thread_ids: set[str]) -> None:
    """删除不再被主库 run 引用的 LangGraph checkpoint 线程。

    在级联删除提交主库之后调用，清理已删 task / workspace 遗留的 checkpoint 线程。
    checkpoint 库与主库不是同一个事务，故 GC 失败不影响已提交的主库删除，仅记录可排查日志，
    后续启动流程可再次执行回收。

    参数:
        thread_ids: 待清理的 checkpoint thread id 集合；为空时直接返回。

    返回:
        无。

    异常:
        不向外抛出；checkpoint 库未初始化或删除失败时记录 ``error`` 级日志。

    副作用:
        从 checkpoint sqlite 的 ``writes`` / ``checkpoints`` 表删除匹配 thread 的行。
    """

    if not thread_ids:
        return
    try:
        checkpoint_file = Path(checkpoint_path())
    except RuntimeError:
        log.warning(
            "task_checkpoint_gc_skipped",
            extra={
                "msg": "checkpoint 存储尚未初始化，跳过已删除 task 的 checkpoint GC",
                "data": {"thread_count": len(thread_ids)},
            },
        )
        return
    if not checkpoint_file.exists():
        return

    try:
        with sqlite3.connect(checkpoint_file) as connection:
            for thread_id in thread_ids:
                connection.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
                connection.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)
                )
    except sqlite3.Error:
        # checkpoint GC 与主库不是同一个事务；主库删除已经提交时，保留可观测
        # 的失败结果，后续启动流程可以再次执行 GC。
        log.exception(
            "task_checkpoint_gc_failed",
            extra={
                "msg": "已删除 task 的 checkpoint GC 失败，主库删除已提交",
                "data": {
                    "thread_count": len(thread_ids),
                    "checkpoint_file": str(checkpoint_file),
                },
            },
        )

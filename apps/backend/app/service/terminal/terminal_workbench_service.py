"""终端工作台的 HTTP 用例：用户新建与关闭会话。

只做 HTTP 用户路径的编排（Task 归属与类型判定、解析工作区根目录），会话生命周期、容量与归属校验
都归 ``TerminalSessionService``；新建或关闭后的会话列表经其变化通知投影进 snapshot，这里不发布。
阻塞调用（worker 启动、进程树清理）由 API 层放进工作线程，本类方法都是同步的。

不进 Task 闸门：用户会话与 Run 调度没有需要互斥的不变量；与 Task 删除的竞争由终端服务的
「Task 正在关闭」状态拒绝（``TerminalSessionStateError``）。
"""

from __future__ import annotations

from app.config.logging.logger import log
from app.service.depends import (
    get_task_service,
    get_terminal_session_service,
    get_workspace_service,
)

# 只有侧栏对话 Task 有终端工作台；子 Agent 与 Team 节点 Task 不开放终端（与工具剥离一致）。
_WORKBENCH_TASK_TYPES = frozenset({"user", "fork"})


class TerminalWorkbenchTaskTypeError(Exception):
    """Task 不是侧栏对话 Task，不支持终端工作台；API 映射为 409。"""

    code = "TERMINAL_TASK_UNSUPPORTED"
    retryable = False


class TerminalWorkbenchService:
    """无状态门面：每次调用按需取进程单例。"""

    def start_user_session(self, task_id: int, *, shell: str) -> str:
        """为侧栏对话 Task 新建一个用户会话，返回 ``sessionId``。

        异常:
            KeyError: Task 或其工作区不存在。
            TerminalWorkbenchTaskTypeError: Task 不是 ``user`` / ``fork``。
            TerminalSessionError: 终端服务拒绝（shell 不可用、容量、状态、worker 不可用）。
        """
        task = get_task_service().get_task(task_id)
        if task.task_type not in _WORKBENCH_TASK_TYPES:
            raise TerminalWorkbenchTaskTypeError(
                f"task type {task.task_type} does not support terminal workbench"
            )
        workspace = get_workspace_service().get_workspace(task.workspace_id)
        runtime = get_terminal_session_service().start(
            task_id=task_id,
            workspace_root=workspace.root_path,
            shell=shell,
            created_by="user",
            run_id=None,
        )
        session_id = runtime.session_id
        log.info(
            "terminal_user_session_started",
            extra={
                "msg": "用户在工作台新建终端会话",
                "data": {"task_id": task_id, "session_id": session_id, "shell": shell},
            },
        )
        return session_id

    def close_session(self, task_id: int, session_id: str) -> None:
        """关闭本 Task 的一个会话（用户或 Agent 创建的都可关）；重复关闭由终端服务幂等处理。

        异常:
            TerminalSessionError: 会话不存在或不属于该 Task、清理失败等。
        """
        get_terminal_session_service().close(session_id, task_id=task_id, by="user")
        log.info(
            "terminal_user_session_closed",
            extra={
                "msg": "用户在工作台关闭终端会话",
                "data": {"task_id": task_id, "session_id": session_id},
            },
        )

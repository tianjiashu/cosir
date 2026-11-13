import { useCallback } from "react";
import { useNavigate } from "react-router-dom";

import { getTask } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { useWorkbenchActions } from "@/lib/workbench/workbench-context";

/** 侧栏里的对话 Task；其余（委派子 Agent）只能在工作台里读。 */
const SIDEBAR_TASK_TYPES = new Set(["user", "user_fork", "agent_fork"]);

/**
 * 打开另一个 Task：侧栏对话按路由跳转，子 Agent 在工作台打开。Agent 消息的发送方、
 * `send_message` 卡片的对方都经这里。
 *
 * 对方是哪种 Task 在点击时向后端读一次，不随快照下发：快照只带 Task id 与显示名，任务类型留在它
 * 唯一的事实源里。
 */
export function useOpenTask() {
  const navigate = useNavigate();
  const workbench = useWorkbenchActions();
  return useCallback(async (taskId: number, name: string) => {
    try {
      const task = await getTask(taskId);
      if (SIDEBAR_TASK_TYPES.has(task.task_type)) {
        navigate(`/tasks/${taskId}`);
      } else {
        workbench?.openAgent({ taskId, title: name });
      }
    } catch (error) {
      void frontendLog("WARNING", "task_open_failed", "打开另一个 Task 失败", { data: { taskId }, error });
      notify.error("无法打开任务", { error, fallback: "任务可能已被删除" });
    }
  }, [navigate, workbench]);
}

import { useCallback } from "react";
import { useNavigate } from "react-router-dom";

import { getSession } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { useWorkbenchActions } from "@/lib/workbench/workbench-context";

/** 侧栏里的对话 Session；其余（委派子 Agent）只能在工作台里读。 */
const SIDEBAR_SESSION_TYPES = new Set(["user", "user_fork", "agent_fork"]);

/**
 * 打开另一个 Session：侧栏会话按路由跳转，子 Agent 在工作台打开。Agent 消息的发送方、
 * `send_message` 卡片的对方都经这里。
 *
 * 对方是哪种 Session 在点击时向后端读一次，不随快照下发：快照只带 Session id 与显示名，会话类型留在它
 * 唯一的事实源里。
 */
export function useOpenSession() {
  const navigate = useNavigate();
  const workbench = useWorkbenchActions();
  return useCallback(async (sessionId: number, name: string) => {
    try {
      const session = await getSession(sessionId);
      if (SIDEBAR_SESSION_TYPES.has(session.session_type)) {
        navigate(`/sessions/${sessionId}`);
      } else {
        workbench?.openAgent({ sessionId, title: name });
      }
    } catch (error) {
      void frontendLog("WARNING", "session_open_failed", "打开另一个 Session 失败", { data: { sessionId }, error });
      notify.error("无法打开会话", { error, fallback: "会话可能已被删除" });
    }
  }, [navigate, workbench]);
}

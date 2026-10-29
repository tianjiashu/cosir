import { useCallback, useEffect, useRef, useState } from "react";

import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import { cancelToolCall, submitToolDecision } from "@/lib/assistant/task-commands";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { closeTerminalSession, createTerminalSession } from "@/lib/terminal/terminal-api";
import type { TerminalNotice } from "@/lib/terminal/terminal-pane-status";
import { isActiveTerminalSession, type TerminalTab } from "@/lib/terminal/terminal-sessions";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";

const NOTICE_MS = 4000;

/**
 * 用户新建 / 关闭终端会话、接管 Agent 输入与交还交接：HTTP 命令、失败短提示、日志与之后的选中 / 收起。
 *
 * 会话列表只读 snapshot：命令成功后不改本地列表，等 snapshot 回推；这里只调整选中与面板开合。
 */
export function useTerminalSessionActions(
  taskId: number,
  sessions: readonly TerminalSessionEntry[] | null,
  tabs: readonly TerminalTab[],
) {
  const [creating, setCreating] = useState(false);
  const [returningHandoff, setReturningHandoff] = useState(false);
  const [notice, setNotice] = useState<TerminalNotice | null>(null);
  const noticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const creatingRef = useRef(false);

  const flashNotice = useCallback((next: TerminalNotice) => {
    if (noticeTimerRef.current !== null) clearTimeout(noticeTimerRef.current);
    setNotice(next);
    noticeTimerRef.current = setTimeout(() => {
      noticeTimerRef.current = null;
      setNotice(null);
    }, NOTICE_MS);
  }, []);
  useEffect(() => () => {
    if (noticeTimerRef.current !== null) clearTimeout(noticeTimerRef.current);
  }, []);

  const createSession = useCallback(async (shell: string) => {
    if (creatingRef.current) return;
    creatingRef.current = true;
    setCreating(true);
    try {
      const { sessionId } = await createTerminalSession(taskId, shell);
      void frontendLog("INFO", "terminal_session_created", "用户新建终端会话", { data: { taskId, sessionId, shell } });
      useTerminalWorkbenchStore.getState().select(taskId, sessionId);
    } catch (error) {
      void frontendLog("WARNING", "terminal_session_create_failed", "新建终端会话失败", { data: { taskId, shell }, error });
      flashNotice({ text: userErrorMessage(error, "新建终端失败"), tone: "destructive" });
    } finally {
      creatingRef.current = false;
      setCreating(false);
    }
  }, [flashNotice, taskId]);

  const closeTab = async (tab: TerminalTab) => {
    const { session } = tab;
    if (!isActiveTerminalSession(session)) {
      // 已结束会话只是从标签栏收回「已结束」菜单，不再发请求。
      const next = (sessions ?? []).find((candidate) => isActiveTerminalSession(candidate))?.sessionId ?? null;
      useTerminalWorkbenchStore.getState().select(taskId, next);
      return;
    }
    // 只有确知空闲（集成就绪且没有执行中的命令）才直接关；无集成时无法判断，仍按有程序运行确认。
    const idle = session.integration === "ready" && !session.commandRunning;
    if (idle) {
      try {
        await closeTerminalSession(taskId, session.sessionId);
      } catch (error) {
        void frontendLog("WARNING", "terminal_session_close_failed", "关闭终端会话失败", { data: { taskId, sessionId: session.sessionId }, error });
        flashNotice({ text: userErrorMessage(error, "关闭终端失败"), tone: "destructive" });
        return;
      }
    } else {
      const confirmed = await requestConfirmation({
        title: "关闭终端？",
        description: session.commandRunning
          ? `「${tab.title}」中有正在运行的程序，关闭会将其结束，此操作不可撤销。`
          : `无法判断「${tab.title}」中是否有程序在运行（shell 集成未就绪），关闭会结束其中的全部程序。`,
        confirmLabel: "关闭终端",
        tone: "destructive",
        icon: "delete",
        failureFallback: "关闭终端失败",
        action: () => closeTerminalSession(taskId, session.sessionId),
      });
      if (!confirmed) return;
    }
    void frontendLog("INFO", "terminal_session_closed", "用户关闭终端会话", { data: { taskId, sessionId: session.sessionId } });
    // 用户主动关闭的会话不留在标签栏（与「正在看的会话自己退出」不同），选中相邻的活跃会话；
    // 关掉的是最后一个活跃会话时收起面板（同 VS Code），再次打开会自动新建，不留空面板。
    const store = useTerminalWorkbenchStore.getState();
    const next = tabs.find((candidate) => candidate.session.sessionId !== session.sessionId && isActiveTerminalSession(candidate.session));
    if (!next) {
      store.select(taskId, null);
      store.setOpen(taskId, false);
    } else if (store.tasks[taskId]?.activeSessionId === session.sessionId) {
      store.select(taskId, next.session.sessionId);
    }
  };

  /**
   * 用户接管 Agent 正在输入的会话：取消 Agent 这次输入的工具调用。终端条件等待只在前台进行，取消后
   * 等待以 `cancelled` 结束、带回截至当时的输出，输入租约随之释放（经 snapshot 推送），然后把焦点给到
   * 该终端。已取消过时照常聚焦，等租约释放即可输入。
   */
  const takeOverInput = useCallback(async (session: TerminalSessionEntry) => {
    if (session.agentInputCallId === null) return;
    const result = await cancelToolCall(taskId, session.agentInputCallId);
    if (result.kind === "failed") {
      flashNotice({ text: result.message, tone: "destructive" });
      return;
    }
    useTerminalWorkbenchStore.getState().select(taskId, session.sessionId);
  }, [flashNotice, taskId]);

  /**
   * 交还交接：交接是 HITL 调用（`terminal_handoff`），交还即对它作出决定；成功后交接提示随 snapshot
   * 消失，Agent 被唤醒。已提交或已不在等待（409）只是重复点击或会话刚结束，平静提示即可。
   */
  const returnHandoff = useCallback(async (session: TerminalSessionEntry) => {
    if (session.handoff === null || returningHandoff) return;
    setReturningHandoff(true);
    try {
      await submitToolDecision(taskId, session.handoff.toolCallId, { decision: "approve" });
      void frontendLog("INFO", "terminal_handoff_returned", "用户交还终端输入权", {
        data: { taskId, sessionId: session.sessionId, toolCallId: session.handoff.toolCallId },
      });
    } catch (error) {
      const conflict = error instanceof HttpError && error.status === 409;
      flashNotice({
        text: conflict ? "交接已处理，稍后会同步" : userErrorMessage(error, "交还失败，请稍后重试"),
        tone: conflict ? "muted" : "destructive",
      });
    } finally {
      setReturningHandoff(false);
    }
  }, [flashNotice, returningHandoff, taskId]);

  return { creating, notice, createSession, closeTab, takeOverInput, returnHandoff, returningHandoff };
}

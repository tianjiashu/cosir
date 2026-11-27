import { useEffect, useState } from "react";

import { markSessionRead } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";
import { onWindowAttentionChange, windowAttentive } from "@/lib/window-attention";
import { useSessionStatusStore } from "./session-status-store";

/**
 * 你正看着的会话有未读结果时告诉后端「看过了」。「正在看」只有这里知道：会话页在切到配置页时保活、订阅还在，
 * 后端分不出你在不在看，所以未读由 Run 落定时照记，由这里清。
 *
 * `sessionId` 是正在显示的会话页（没有显示会话页时为 null）。三个时刻会清：打开会话页、窗口回到前台、
 * 正看着时这个会话的 Run 落定（推送带来 `unread`）。请求失败只记日志，等下一次时刻再试；结果经 Session 推送回来，
 * 不乐观改状态。
 *
 * 「看过」按「你停在这个会话上」算，有意不等正文加载或渲染出来：未读提醒的用途是告诉你「有个会话出了结果你还不知道」，
 * 你已经点进来就知道了；正文加载失败时页面顶部有连接横幅，你就在这个会话里、能重试。侧栏提醒用同一定义
 * （`session-attention.ts` 的 `viewing`：停在这个会话上就不显示），两处必须一致——要改成「首帧正文渲染后才算」
 * 得两处一起改，只改这里会出现「标记不显示、库里却没清」。
 */
export function useMarkSessionRead(sessionId: number | null): void {
  const unread = useSessionStatusStore((state) => (sessionId === null ? false : state.sessions[sessionId]?.unread ?? false));
  const [visible, setVisible] = useState(windowAttentive);

  useEffect(() => onWindowAttentionChange(() => setVisible(windowAttentive())), []);

  useEffect(() => {
    if (sessionId === null || !unread || !visible) return;
    markSessionRead(sessionId).catch((error: unknown) => {
      void frontendLog("WARNING", "session_mark_read_failed", "标记会话已读失败", { data: { session_id: sessionId }, error });
    });
  }, [sessionId, unread, visible]);
}

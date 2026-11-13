import { useCallback, useRef } from "react";
import { useHotkeys } from "react-hotkeys-hook";

import { countRunning, useBackgroundTasks } from "@/lib/background-tasks/background-tasks-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { APP_HOTKEY_OPTIONS, APP_SHORTCUTS } from "@/lib/keyboard/shortcuts";
import { selectConversationAsideVisible, useConversationAsideStore } from "@/lib/conversation-aside/conversation-aside-store";

import { useConversationAsideHasContent } from "./aside-card-registry";

/**
 * 会话侧栏在 AppLayout 里的接线：页头按钮、快捷键与侧栏本身共用这一份开关。
 *
 * - 开关不随 Session 切换：宽屏是全局偏好（默认显示），窄屏浮层只响应用户操作（见 `useConversationAsideStore`）；
 *   `sessionId` 为 null（不在对话页）时快捷键停用，侧栏与按钮由调用方不渲染。
 * - 摆放不在这里：侧栏在对话视口里，按视口宽度自己决定（见 `ConversationAsidePanel`）。
 * - `close` 用于侧栏内部的关闭（浮层 Esc、崩溃兜底的离开按钮）：焦点原本在即将卸载的侧栏里，
 *   不交还就会落到 body，所以关闭后把焦点交给挂了 `buttonRef` 的页头按钮。
 * - `runningCount` 是页头角标：当前 Session 运行中的后台工作数；有新条目时不自动打开侧栏。
 * - `hasContent`：没有任何卡片有内容时，侧栏与页头按钮都不出现、快捷键停用（侧栏自己按同一判定不渲染）。
 *   开关偏好不变：用户收起过，有了内容也保持收起，只亮页头角标。
 */
export function useConversationAside(sessionId: number | null) {
  const visible = useConversationAsideStore(selectConversationAsideVisible);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const runningCount = countRunning(useBackgroundTasks(sessionId));
  const hasContent = useConversationAsideHasContent(sessionId);

  const setVisible = useCallback((next: boolean) => {
    useConversationAsideStore.getState().setVisible(next);
    void frontendLog("INFO", "conversation_aside_toggled", next ? "会话侧栏已打开" : "会话侧栏已收起", { data: { sessionId, open: next } });
  }, [sessionId]);
  const toggle = useCallback(() => setVisible(!visible), [visible, setVisible]);
  const close = useCallback(() => {
    setVisible(false);
    buttonRef.current?.focus();
  }, [setVisible]);
  useHotkeys(APP_SHORTCUTS.toggleConversationAside.keys, toggle, { ...APP_HOTKEY_OPTIONS, enabled: sessionId !== null && hasContent }, [toggle]);

  return { visible, runningCount, hasContent, buttonRef, toggle, close };
}

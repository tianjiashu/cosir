import { useCallback, useRef } from "react";
import { useHotkeys } from "react-hotkeys-hook";

import { countRunning, useBackgroundTasks } from "@/lib/background-tasks/background-tasks-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { APP_HOTKEY_OPTIONS, APP_SHORTCUTS } from "@/lib/keyboard/shortcuts";
import { selectTaskRailVisible, useTaskRailStore } from "@/lib/task-rail/task-rail-store";

/**
 * 会话侧栏在 AppLayout 里的接线：页头按钮、快捷键与侧栏本身共用这一份开关。
 *
 * - 开关不随 Task 切换：宽屏是全局偏好（默认显示），窄屏浮层只响应用户操作（见 `useTaskRailStore`）；
 *   `taskId` 为 null（不在对话页）时快捷键停用，侧栏与按钮由调用方不渲染。
 * - 摆放不在这里：侧栏在对话视口里，按视口宽度自己决定（见 `TaskRail`）。
 * - `close` 用于侧栏内部的关闭（浮层 Esc、崩溃兜底的离开按钮）：焦点原本在即将卸载的侧栏里，
 *   不交还就会落到 body，所以关闭后把焦点交给挂了 `buttonRef` 的页头按钮。
 * - `runningCount` 是页头角标：当前 Task 运行中的后台工作数；有新条目时不自动打开侧栏。
 */
export function useTaskRail(taskId: number | null) {
  const visible = useTaskRailStore(selectTaskRailVisible);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const runningCount = countRunning(useBackgroundTasks(taskId));

  const setVisible = useCallback((next: boolean) => {
    useTaskRailStore.getState().setVisible(next);
    void frontendLog("INFO", "task_rail_toggled", next ? "会话侧栏已打开" : "会话侧栏已收起", { data: { taskId, open: next } });
  }, [taskId]);
  const toggle = useCallback(() => setVisible(!visible), [visible, setVisible]);
  const close = useCallback(() => {
    setVisible(false);
    buttonRef.current?.focus();
  }, [setVisible]);
  useHotkeys(APP_SHORTCUTS.toggleTaskRail.keys, toggle, { ...APP_HOTKEY_OPTIONS, enabled: taskId !== null }, [toggle]);

  return { visible, runningCount, buttonRef, toggle, close };
}

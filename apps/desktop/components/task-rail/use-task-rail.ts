import { useCallback, useRef } from "react";
import { useHotkeys } from "react-hotkeys-hook";

import { useElementSize } from "@/hooks/use-element-size";
import { countRunning, useBackgroundTasks } from "@/lib/background-tasks/store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { APP_HOTKEY_OPTIONS, APP_SHORTCUTS } from "@/lib/shortcuts";
import { taskRailPlacement } from "@/lib/task-rail/placement";
import { useTaskRailStore } from "@/lib/task-rail/store";

/**
 * 会话侧栏在 AppLayout 里的接线：页头按钮、快捷键与侧栏本身共用这一份开关与摆放。
 *
 * - 开关是全局偏好（`useTaskRailStore`），不随 Task 切换；`taskId` 为 null（不在对话页）时快捷键停用，
 *   侧栏与按钮由调用方不渲染。
 * - 摆放按 `rowRef` 所挂的「对话 + 侧栏」整行宽度决定，不量对话区，否则侧栏一固定对话就变窄、
 *   随即又判为浮起，来回抖动（见 `taskRailPlacement`）。
 * - `close` 用于侧栏内部的关闭（浮层 Esc、崩溃兜底的离开按钮）：焦点原本在即将卸载的侧栏里，
 *   不交还就会落到 body，所以关闭后把焦点交给挂了 `buttonRef` 的页头按钮。
 * - `runningCount` 是页头角标：当前 Task 运行中的后台工作数；有新条目时不自动打开侧栏。
 */
export function useTaskRail(taskId: number | null) {
  const open = useTaskRailStore((state) => state.open);
  const [rowRef, { width: rowWidth }] = useElementSize<HTMLDivElement>();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const placement = taskRailPlacement(rowWidth);
  const runningCount = countRunning(useBackgroundTasks(taskId));

  const setOpen = useCallback((next: boolean) => {
    useTaskRailStore.getState().setOpen(next);
    void frontendLog("INFO", "task_rail_toggled", next ? "会话侧栏已打开" : "会话侧栏已收起", { data: { taskId, open: next, placement } });
  }, [placement, taskId]);
  const toggle = useCallback(() => setOpen(!open), [open, setOpen]);
  const close = useCallback(() => {
    setOpen(false);
    buttonRef.current?.focus();
  }, [setOpen]);
  useHotkeys(APP_SHORTCUTS.toggleTaskRail.keys, toggle, { ...APP_HOTKEY_OPTIONS, enabled: taskId !== null }, [toggle]);

  return { open, placement, runningCount, rowRef, buttonRef, toggle, close };
}

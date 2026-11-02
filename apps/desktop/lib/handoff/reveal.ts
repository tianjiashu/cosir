import type { HandoffEntry } from "@/lib/assistant/contract";
import { revealBrowserTab } from "@/lib/browser/panel-store";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";

/**
 * 把交接的资源摆到用户面前：打开对应面板并选中它。新增一种可交接的资源时在这里加一个分支，提示条组件
 * （`components/handoff/handoff-banner.tsx`）放进那个资源的面板即可。
 */
export function revealHandoff(taskId: number, entry: HandoffEntry): void {
  switch (entry.surface) {
    case "terminal":
      useTerminalWorkbenchStore.getState().reveal(taskId, entry.resourceId);
      return;
    case "browser":
      revealBrowserTab(taskId, entry.resourceId);
      return;
  }
}

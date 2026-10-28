const COLLAPSED_KEY = "cosir:task-overview:background-tasks-collapsed";

/** 读取「后台任务」卡是否折叠的偏好；未保存或存储不可用时展开。 */
export function readBackgroundTasksCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

/** 写入折叠偏好；纯 UI 偏好，不承载任务事实。 */
export function writeBackgroundTasksCollapsed(collapsed: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    return;
  }
}

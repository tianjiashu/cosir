const LAST_WORKSPACE_KEY = "cosir:last-workspace-id";

export function readLastWorkspaceId(): number | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(LAST_WORKSPACE_KEY);
  const value = raw === null ? NaN : Number(raw);
  return Number.isInteger(value) && value > 0 ? value : null;
}

export function writeLastWorkspaceId(workspaceId: number): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(LAST_WORKSPACE_KEY, String(workspaceId));
}

const SIDEBAR_COLLAPSED_KEY = "cosir:sidebar-collapsed";

/** 宽屏下侧栏是否收起的用户偏好；窄屏始终以图标栏 + 抽屉呈现，不读取它。 */
export function readSidebarCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "true";
  } catch {
    return false;
  }
}

export function writeSidebarCollapsed(collapsed: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(collapsed));
  } catch {
    // 偏好写入失败（隐私模式等）只影响下次启动的默认状态，不影响当前会话。
  }
}

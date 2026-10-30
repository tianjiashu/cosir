/**
 * 最近工作区：启动时恢复上次选中的工作区，属于导航记忆，只由工作区目录（`use-workspace-catalog`）
 * 读写；界面尺寸偏好在 `lib/layout-preferences.ts`。
 */
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

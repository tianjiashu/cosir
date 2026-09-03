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

export function clearLastWorkspaceId(workspaceId?: number): void {
  if (typeof window === "undefined") return;
  if (workspaceId === undefined || readLastWorkspaceId() === workspaceId) {
    window.localStorage.removeItem(LAST_WORKSPACE_KEY);
  }
}

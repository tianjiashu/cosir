const STORAGE_KEY_PREFIX = "cosir:tool-group-selection:task:";

function storageKey(taskId: number): string {
  return `${STORAGE_KEY_PREFIX}${taskId}`;
}

/** 读取任务级禁用工具组偏好；未保存或存储不可用时返回 null。 */
export function readStoredToolGroupSelection(taskId: number): string[] | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(storageKey(taskId));
    if (raw === null) return null;
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value) || !value.every((group) => typeof group === "string" && group.length > 0)) {
      return null;
    }
    return [...new Set(value)];
  } catch {
    return null;
  }
}

/** 写入任务级禁用工具组偏好；该偏好只影响前端初始选择，不承载 Run 事实。 */
export function writeStoredToolGroupSelection(taskId: number, groups: readonly string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey(taskId), JSON.stringify([...new Set(groups)]));
  } catch {
    return;
  }
}

/** 清理未能创建成功的临时任务对应的前端偏好。 */
export function removeStoredToolGroupSelection(taskId: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(storageKey(taskId));
  } catch {
    return;
  }
}

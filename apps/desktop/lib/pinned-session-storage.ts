/**
 * 侧栏置顶会话偏好的读取与写入契约。
 *
 * 负责什么：持久化用户置顶的 sessionId 列表（最近置顶的在前），供侧栏决定摆放位置。
 * 不负责什么：不是 Session 事实，后端不知道置顶；不校验 id 是否仍存在——Session id 由 SQLite
 * `sqlite_autoincrement` 分配、删除后不复用，残留 id 由侧栏渲染时忽略即可，无需清理。
 * 副作用：读写 渲染进程 localStorage；存储不可用时读取返回空列表，写入返回 false。
 */

const STORAGE_KEY = "cosir:pinned-sessions:v1";
const EMPTY_IDS: readonly number[] = [];
let cachedRaw: string | null | undefined;
let cachedIds = EMPTY_IDS;
const subscribers = new Set<() => void>();

function notifySubscribers(): void {
  subscribers.forEach((listener) => listener());
}

function handleStorage(event: StorageEvent): void {
  if (event.key !== STORAGE_KEY && event.key !== null) return;
  cachedRaw = undefined;
  notifySubscribers();
}

/**
 * 读取置顶 sessionId 列表；同一份原始值返回同一个数组引用，满足 `useSyncExternalStore` 的稳定快照要求。
 *
 * @returns 去重后的正整数 id，最近置顶的在前；SSR、损坏数据或存储不可用时返回空数组。
 */
export function readPinnedSessionIds(): readonly number[] {
  if (typeof window === "undefined") return EMPTY_IDS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === cachedRaw) return cachedIds;
    cachedRaw = raw;
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    cachedIds = Array.isArray(parsed)
      ? [...new Set(parsed.filter((id): id is number => Number.isInteger(id) && id > 0))]
      : EMPTY_IDS;
    return cachedIds;
  } catch {
    return EMPTY_IDS;
  }
}

/** Subscribe to same-window writes and cross-window storage changes. */
export function subscribePinnedSessionIds(listener: () => void): () => void {
  subscribers.add(listener);
  if (subscribers.size === 1) window.addEventListener("storage", handleStorage);
  return () => {
    subscribers.delete(listener);
    if (subscribers.size === 0) window.removeEventListener("storage", handleStorage);
  };
}

/**
 * 置顶或取消置顶一个会话；置顶总是移到最前。
 *
 * @returns 写入成功时为 true；浏览器存储不可用或配额不足时为 false。
 */
export function setSessionPinned(sessionId: number, pinned: boolean): boolean {
  if (typeof window === "undefined") return false;
  const rest = readPinnedSessionIds().filter((id) => id !== sessionId);
  const next = pinned ? [sessionId, ...rest] : rest;
  try {
    const raw = JSON.stringify(next);
    window.localStorage.setItem(STORAGE_KEY, raw);
    cachedRaw = raw;
    cachedIds = next;
    notifySubscribers();
    return true;
  } catch {
    return false;
  }
}

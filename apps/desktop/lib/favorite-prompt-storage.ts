/**
 * 本机常用提示词偏好的读取与写入契约。
 *
 * 负责什么：校验并持久化用户可复用的提示词列表，避免组件直接操作 localStorage。
 * 不负责什么：不管理任务或对话事实，不记录提示词内容日志，也不负责输入框插入。
 * 副作用：读写 WebView localStorage；存储不可用时读取返回空列表，写入返回 false。
 */

export type FavoritePrompt = {
  id: string;
  title: string;
  content: string;
};

const STORAGE_KEY = "cosir:favorite-prompts:v1";
const EMPTY_PROMPTS: FavoritePrompt[] = [];
let cachedRaw: string | null | undefined;
let cachedPrompts = EMPTY_PROMPTS;
const subscribers = new Set<() => void>();

function notifySubscribers(): void {
  subscribers.forEach((listener) => listener());
}

function handleStorage(event: StorageEvent): void {
  if (event.key !== STORAGE_KEY && event.key !== null) return;
  cachedRaw = undefined;
  notifySubscribers();
}

function isFavoritePrompt(value: unknown): value is FavoritePrompt {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.id === "string"
    && record.id.length > 0
    && typeof record.title === "string"
    && record.title.trim().length > 0
    && typeof record.content === "string"
    && record.content.trim().length > 0;
}

/**
 * 读取并校验提示词列表。
 *
 * @returns 有效提示词；SSR、损坏数据或浏览器存储不可用时返回空数组。
 */
export function readFavoritePrompts(): FavoritePrompt[] {
  if (typeof window === "undefined") return EMPTY_PROMPTS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === cachedRaw) return cachedPrompts;
    cachedRaw = raw;
    if (!raw) {
      cachedPrompts = EMPTY_PROMPTS;
      return cachedPrompts;
    }
    const parsed: unknown = JSON.parse(raw);
    cachedPrompts = Array.isArray(parsed) ? parsed.filter(isFavoritePrompt) : EMPTY_PROMPTS;
    return cachedPrompts;
  } catch {
    return EMPTY_PROMPTS;
  }
}

/** Subscribe to same-window writes and cross-window storage changes. */
export function subscribeFavoritePrompts(listener: () => void): () => void {
  subscribers.add(listener);
  if (subscribers.size === 1) window.addEventListener("storage", handleStorage);
  return () => {
    subscribers.delete(listener);
    if (subscribers.size === 0) window.removeEventListener("storage", handleStorage);
  };
}

/**
 * 持久化整份提示词列表。
 *
 * @param prompts - 按界面显示顺序排列的提示词。
 * @returns 写入成功时为 true；浏览器存储不可用或配额不足时为 false。
 */
export function writeFavoritePrompts(prompts: readonly FavoritePrompt[]): boolean {
  if (typeof window === "undefined" || !prompts.every(isFavoritePrompt)) return false;
  try {
    const raw = JSON.stringify(prompts);
    window.localStorage.setItem(STORAGE_KEY, raw);
    cachedRaw = raw;
    cachedPrompts = [...prompts];
    notifySubscribers();
    return true;
  } catch {
    return false;
  }
}

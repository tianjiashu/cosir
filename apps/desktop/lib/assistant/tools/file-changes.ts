/** 一次工具调用的文件操作；index 保留原始操作序号，同路径的多次修改不能合并。 */
export type FileChange = Readonly<{
  index: number;
  path: string;
  status: "added" | "modified";
  patch: string | null;
  insertions: number;
  deletions: number;
}>;

const cache = new WeakMap<readonly unknown[], readonly FileChange[]>();
const EMPTY: readonly FileChange[] = [];
const count = (value: unknown): number => typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.trunc(value)) : 0;

/**
 * file-changes 展示载荷唯一解释边界：忽略没有路径的条目，统一状态与计数。
 * Transport 按路径不可变更新；按原 changes 数组缓存，使汇总和过程详情共用同一操作对象。
 */
export function readFileChanges(value: unknown): readonly FileChange[] {
  if (!Array.isArray(value)) return EMPTY;
  const cached = cache.get(value);
  if (cached) return cached;
  const changes: FileChange[] = [];
  value.forEach((item: unknown, index) => {
    if (!item || typeof item !== "object" || !("path" in item) || typeof item.path !== "string") return;
    const change = item as Record<string, unknown>;
    changes.push({
      index, path: item.path,
      status: change.status === "added" ? "added" : "modified",
      patch: typeof change.patch === "string" ? change.patch : null,
      insertions: count(change.insertions), deletions: count(change.deletions),
    });
  });
  cache.set(value, changes);
  return changes;
}

/**
 * 模型选择持久化读写（localStorage）。
 *
 * 这是「模型选择」存储契约的唯一真相来源：key 构造、类型定义、解析与
 * 合法性判断都收口在此文件，写入侧（model-selector.tsx）与读取侧
 * （assistant.tsx）都必须引用本模块，禁止各自重写 key 构造或解析逻辑。
 */

import { REASONING_EFFORT_VALUES } from "@/lib/model-selection-constants";

export type ModelSelection = {
  providerId: number;
  modelName: string;
  reasoningEffort: (typeof REASONING_EFFORT_VALUES)[number] | null;
};

export type ModelSelectionScope =
  | { kind: "workspace"; id: number }
  | { kind: "task"; id: number };

const STORAGE_KEY_PREFIX = "cosir:model-selection:";
const DEFAULT_TASK_KEY = "default";
const selectionSnapshotCache = new Map<string, {
  raw: string | null;
  value: Partial<ModelSelection> | null;
}>();
const selectionSubscribers = new Map<string, Set<() => void>>();

function getCachedSelection(scope?: ModelSelectionScope): Partial<ModelSelection> | null {
  if (typeof window === "undefined") return null;
  const key = selectionStorageKey(scope);
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(key);
  } catch {
    return null;
  }
  const cached = selectionSnapshotCache.get(key);
  if (cached?.raw === raw) return cached.value;
  const value = parseStoredSelection(raw);
  selectionSnapshotCache.set(key, { raw, value });
  return value;
}

function notifySelection(scope?: ModelSelectionScope): void {
  for (const listener of selectionSubscribers.get(selectionStorageKey(scope)) ?? []) listener();
}

/**
 * 构造 localStorage 中模型选择项的 key。工作区与任务即使使用同一个数字
 * ID，也必须生成不同的 key，避免页面级选择污染任务级选择。
 */
export function scopeSuffix(scope?: ModelSelectionScope): string {
  return scope ? `${scope.kind}:${scope.id}` : "";
}

export function selectionStorageKey(scope?: ModelSelectionScope): string {
  const suffix = scopeSuffix(scope);
  return `${STORAGE_KEY_PREFIX}${suffix || DEFAULT_TASK_KEY}`;
}

/**
 * 校验未知值是否为合法的 `reasoningEffort` 取值（类型谓词）。
 *
 * @param value - 待校验的任意值（通常来自 localStorage 解析后的字段）。
 * @returns 类型谓词：为 true 时 TS 收窄 `value` 为
 *   ModelSelection["reasoningEffort"]（即 "low" | "high" | "max" | null），
 *   合法取值为 null / "low" / "high" / "max"。
 * @throws 永不抛异常。校验失败（非上述任一取值）直接返回 false。
 */
function isReasoningEffort(
  value: unknown,
): value is ModelSelection["reasoningEffort"] {
  return value === null || REASONING_EFFORT_VALUES.includes(value as "low" | "high" | "max");
}

/**
 * 解析 localStorage 中的原始字符串为模型选择对象。
 *
 * 返回 Partial<ModelSelection>。解析失败（raw 为空、
 * JSON.parse 异常、字段缺失或类型不符）一律返回 null，不抛异常。
 */
export function parseStoredSelection(
  raw: string | null,
): Partial<ModelSelection> | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    if (
      typeof value.providerId !== "number" ||
      !Number.isInteger(value.providerId) ||
      typeof value.modelName !== "string" ||
      !value.modelName ||
      !isReasoningEffort(value.reasoningEffort)
    ) {
      return null;
    }
    return {
      providerId: value.providerId,
      modelName: value.modelName,
      reasoningEffort: value.reasoningEffort,
    };
  } catch {
    return null;
  }
}

/**
 * 读取指定作用域的模型选择结果。
 *
 * 不在 workspace 与 task 之间回落。未提供作用域时只读取显式 default key。
 * 任何失败路径（window 不存在、localStorage 被禁用、JSON.parse 失败、字段
 * 缺失或类型不符）均返回 null，不抛异常。
 */
export function getStoredSelectionSnapshot(
  scope?: ModelSelectionScope,
): Partial<ModelSelection> | null {
  return getCachedSelection(scope);
}

export function readStoredSelection(
  scope?: ModelSelectionScope,
): Partial<ModelSelection> | null {
  return getStoredSelectionSnapshot(scope);
}

export function subscribeStoredSelection(
  scope: ModelSelectionScope,
  listener: () => void,
): () => void {
  const key = selectionStorageKey(scope);
  const listeners = selectionSubscribers.get(key) ?? new Set<() => void>();
  listeners.add(listener);
  selectionSubscribers.set(key, listeners);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) selectionSubscribers.delete(key);
  };
}

/**
 * 写入指定任务或工作区的模型选择结果。
 *
 * @param scope - 当前选择所属的明确作用域。
 * @param selection - 要持久化的完整模型选择。
 * @returns 无。
 * @sideEffects 在浏览器 localStorage 中写入模型选择；浏览器不可用时静默跳过。
 */
export function writeStoredSelection(
  scope: ModelSelectionScope,
  selection: ModelSelection,
): void {
  if (typeof window === "undefined") return;
  const key = selectionStorageKey(scope);
  const raw = JSON.stringify(selection);
  try {
    if (window.localStorage.getItem(key) === raw) return;
    window.localStorage.setItem(key, raw);
  } catch {
    return;
  }
  selectionSnapshotCache.set(key, { raw, value: selection });
  notifySelection(scope);
}

/**
 * 模型选择持久化读写（localStorage）。
 *
 * 这是「模型选择」存储契约的唯一真相来源：key 构造、类型定义、解析与
 * 合法性判断都收口在此文件，写入侧（model-selector.tsx）与读取侧
 * （assistant.tsx）都必须引用本模块，禁止各自重写 key 构造或解析逻辑。
 */

export type ModelSelection = {
  providerId: number;
  modelName: string;
  reasoningEffort: "low" | "high" | "max" | null;
};

const STORAGE_KEY_PREFIX = "cosir:model-selection:";
const DEFAULT_TASK_KEY = "default";

/**
 * 构造 localStorage 中模型选择项的 key。
 *
 * taskId 缺省（undefined）时统一回落为 "default"，保证写入侧与读取侧
 * 在 taskId 为空时构造出完全相同的 key，避免「写入 ...:default、
 * 读取 ...:undefined」导致永远读不到的缺陷。
 */
export function selectionStorageKey(taskId?: number): string {
  return `${STORAGE_KEY_PREFIX}${taskId ?? DEFAULT_TASK_KEY}`;
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
  return value === null || value === "low" || value === "high" || value === "max";
}

/**
 * 解析 localStorage 中的原始字符串为模型选择对象。
 *
 * 返回 Partial<ModelSelection>：当 taskId 明确但专属 key 缺失、需要回落
 * 到默认 key 时，调用方仍可继续读取默认选择。解析失败（raw 为空、
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
 * 读取指定 task 的模型选择结果。
 *
 * 优先读取 task 专属 key；若 taskId 明确但专属 key 缺失，则回落读取
 * 默认 key（taskId 缺省时直接读默认 key）。任何失败路径（window 不存在、
 * localStorage 被禁用、JSON.parse 失败、字段缺失或类型不符）均返回 null，
 * 不抛异常。
 */
export function readStoredSelection(
  taskId?: number,
): Partial<ModelSelection> | null {
  if (typeof window === "undefined") return null;
  const taskSelection = parseStoredSelection(
    window.localStorage.getItem(selectionStorageKey(taskId)),
  );
  if (taskSelection || taskId === undefined) return taskSelection;
  return parseStoredSelection(
    window.localStorage.getItem(selectionStorageKey()),
  );
}

/**
 * 写入指定任务或工作区的模型选择结果。
 *
 * @param taskId - 当前选择所属的任务或工作区标识。
 * @param selection - 要持久化的完整模型选择。
 * @returns 无。
 * @sideEffects 在浏览器 localStorage 中写入模型选择；浏览器不可用时静默跳过。
 */
export function writeStoredSelection(
  taskId: number,
  selection: ModelSelection,
): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(selectionStorageKey(taskId), JSON.stringify(selection));
}

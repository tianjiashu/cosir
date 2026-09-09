import type { ModelCatalog } from "@/lib/model-catalog";
import type { ModelSelection } from "@/lib/model-selection-storage";

export type ModelContextConfig = {
  modelName?: unknown;
  reasoningEffort?: unknown;
};

const BACKEND_REASONING_EFFORTS = new Set(["low", "high", "max"]);

export function selectionToTransportFields(
  selection: ModelSelection | null | undefined,
): Record<string, unknown> {
  if (!selection) return {};
  return {
    providerId: selection.providerId,
    modelName: selection.modelName,
    reasoningEffort: selection.reasoningEffort,
  };
}

/**
 * 把 assistant-ui ModelContext.config 中的 UI option id 映射为后端字段。
 *
 * UI 只暴露全局目录生成的 option id；后端仍接收自己的 providerId、modelName
 * 和 reasoningEffort，避免把 UI 选择器内部标识泄漏为模型名称。
 */
export function modelContextToTransportFields(
  config: unknown,
  catalog: ModelCatalog | null,
): Record<string, unknown> | null {
  if (!catalog || !config || typeof config !== "object") return null;
  const contextConfig = config as ModelContextConfig;
  if (typeof contextConfig.modelName !== "string") return null;

  const model = catalog.byOptionId.get(contextConfig.modelName);
  if (!model) return null;

  const contextEffort = contextConfig.reasoningEffort;
  const reasoningEffort =
    typeof contextEffort === "string" && BACKEND_REASONING_EFFORTS.has(contextEffort)
      ? contextEffort
      : model.supportsReasoningEffort
        ? "high"
        : null;

  return {
    providerId: model.providerId,
    modelName: model.modelName,
    reasoningEffort,
  };
}

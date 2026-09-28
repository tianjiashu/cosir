import type { ModelCatalog } from "@/lib/model-catalog";
import { REASONING_EFFORT_VALUES } from "@/lib/model-selection-constants";
import type { ModelSelection } from "@/lib/model-selection-storage";

export type ModelContextConfig = {
  modelName?: unknown;
  reasoningEffort?: unknown;
};

const BACKEND_REASONING_EFFORTS = new Set<string>(REASONING_EFFORT_VALUES);

export function selectionToTransportFields(
  selection: ModelSelection | null | undefined,
): Record<string, unknown> {
  if (!selection) return {};
  return {
    modelConfigId: selection.modelConfigId,
    reasoningEffort: selection.reasoningEffort,
  };
}

/**
 * 把 assistant-ui ModelContext.config 中的 UI option id 映射为后端字段。
 *
 * UI 只暴露全局目录生成的 option id；后端只接收模型配置身份，模型名由后端配置事实读取。
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
    modelConfigId: model.modelConfigId,
    reasoningEffort,
  };
}

import { useQuery } from "@tanstack/react-query";

import { getModelConfigs, type ModelConfig } from "@/lib/api/model-configs";
import { queryKeys } from "@/lib/query/query-keys";

export type ModelCatalogModel = {
  optionId: string;
  modelConfigId: number;
  configName: string;
  modelName: string;
  contextWindowK: number;
  label: string;
  supportsReasoningEffort: boolean;
  supportsImage: boolean;
};

export type ModelCatalog = {
  models: readonly ModelCatalogModel[];
  byOptionId: ReadonlyMap<string, ModelCatalogModel>;
};

export type ModelCatalogSnapshot = {
  status: "loading" | "ready" | "error";
  catalog: ModelCatalog | null;
  error: Error | null;
};

export function modelOptionId(modelConfigId: number): string {
  return `model-config:${modelConfigId}`;
}

function mapModel(config: ModelConfig): ModelCatalogModel {
  return {
    optionId: modelOptionId(config.config_id),
    modelConfigId: config.config_id,
    configName: config.config_name,
    modelName: config.model_name,
    contextWindowK: config.context_window_k,
    label: `${config.config_name} · ${config.model_name}`,
    supportsReasoningEffort: config.supports_reasoning_effort,
    supportsImage: config.supports_image,
  };
}

export function buildModelCatalog(configs: readonly ModelConfig[]): ModelCatalog {
  const models = configs.map(mapModel);
  return { models, byOptionId: new Map(models.map((model) => [model.optionId, model])) };
}

/**
 * 启用的模型连接配置目录：与模型配置管理共用 `queryKeys.modelConfigs()` 这一份 react-query 缓存，
 * 保存 / 删除后的失效与后端换代时的全局失效都会自动刷新目录，不另维护一份缓存。
 * 后台重拉期间保留旧目录、状态仍为 ready，选择器不会因刷新回到加载态。
 */
export function useModelCatalog(): ModelCatalogSnapshot & { retry: () => void } {
  const query = useQuery({
    queryKey: queryKeys.modelConfigs(),
    queryFn: ({ signal }) => getModelConfigs({ signal }),
    select: buildModelCatalog,
  });
  return {
    status: query.isError ? "error" : query.isSuccess ? "ready" : "loading",
    catalog: query.data ?? null,
    error: query.error,
    retry: () => void query.refetch(),
  };
}

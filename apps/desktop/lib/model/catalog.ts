import { modelConnectionsQueryOptions } from "@/lib/query/query-options";
import { useQuery } from "@tanstack/react-query";

import { type ModelConfig, type ModelConfigFile, type ModelConnection } from "@/lib/api/model-connections";

export type ModelCatalogModel = {
  optionId: string;
  modelConfigId: string;
  /** 所属连接的名称：选择器的分组标题。 */
  connectionName: string;
  modelName: string;
  contextWindowK: number;
  /** 「连接 · 模型」：不在分组列表里时（如已选值、摘要）用它说清是哪个连接的模型。 */
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

export function modelOptionId(modelConfigId: string): string {
  return `model-config:${modelConfigId}`;
}

/** 配置引用了目录里不存在（已删除或不可用）的模型时的展示名。 */
export function unknownModelName(modelConfigId: string): string {
  return `未知模型（${modelConfigId}）`;
}

function mapModel(connection: ModelConnection, config: ModelConfig): ModelCatalogModel {
  return {
    optionId: modelOptionId(config.config_id),
    modelConfigId: config.config_id,
    connectionName: connection.name,
    modelName: config.model_name,
    contextWindowK: config.context_window_k,
    label: `${connection.name} · ${config.model_name}`,
    supportsReasoningEffort: config.supports_reasoning_effort,
    supportsImage: config.supports_image,
  };
}

/** 按连接顺序、连接内按加入顺序展开成一个列表；同一连接的模型相邻，选择器据此分组。 */
export function buildModelCatalog(connections: readonly ModelConnection[]): ModelCatalog {
  const models = connections.flatMap((connection) => connection.models.map((config) => mapModel(connection, config)));
  return { models, byOptionId: new Map(models.map((model) => [model.optionId, model])) };
}

/** 模块级稳定引用：react-query 的 `select` 换了引用就会重算，内联函数会让每次渲染都得到新目录。 */
function catalogOfFile(file: ModelConfigFile): ModelCatalog {
  return buildModelCatalog(file.connections);
}

/**
 * 可选模型目录：与模型配置管理共用 `queryKeys.modelConnections()` 这一份 react-query 缓存，
 * 保存 / 删除后的失效与后端换代时的全局失效都会自动刷新目录，不另维护一份缓存。
 * 后台重拉期间保留旧目录、状态仍为 ready，选择器不会因刷新回到加载态。
 */
export function useModelCatalog(): ModelCatalogSnapshot & { retry: () => void } {
  const query = useQuery({
    ...modelConnectionsQueryOptions(),
    select: catalogOfFile,
  });
  return {
    status: query.isError ? "error" : query.isSuccess ? "ready" : "loading",
    catalog: query.data ?? null,
    error: query.error,
    retry: () => void query.refetch(),
  };
}

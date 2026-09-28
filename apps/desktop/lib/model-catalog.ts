import { useEffect, useSyncExternalStore } from "react";

import { getModelConfigs, type ModelConfig } from "@/lib/api/model-configs";

export type ModelCatalogModel = {
  optionId: string;
  modelConfigId: number;
  configName: string;
  modelName: string;
  contextWindowK: number;
  label: string;
  supportsReasoningEffort: boolean;
  supportsImage: boolean;
  supportsVideo: boolean;
};

export type ModelCatalog = {
  models: readonly ModelCatalogModel[];
  byOptionId: ReadonlyMap<string, ModelCatalogModel>;
};

export type ModelCatalogSnapshot = {
  status: "idle" | "loading" | "ready" | "error";
  catalog: ModelCatalog | null;
  error: Error | null;
};

const initialSnapshot: ModelCatalogSnapshot = { status: "idle", catalog: null, error: null };
let snapshot = initialSnapshot;
let activeRequest: Promise<ModelCatalog | null> | null = null;
let activeController: AbortController | null = null;
let requestGeneration = 0;
const subscribers = new Set<() => void>();

function notify(): void {
  for (const subscriber of subscribers) subscriber();
}

function setSnapshot(next: ModelCatalogSnapshot): void {
  snapshot = next;
  notify();
}

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
    supportsVideo: config.supports_video,
  };
}

export function buildModelCatalog(configs: readonly ModelConfig[]): ModelCatalog {
  const models = configs.map(mapModel);
  return { models, byOptionId: new Map(models.map((model) => [model.optionId, model])) };
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function getModelCatalogSnapshot(): ModelCatalogSnapshot {
  return snapshot;
}

export function subscribeModelCatalog(listener: () => void): () => void {
  subscribers.add(listener);
  return () => subscribers.delete(listener);
}

/** 加载启用的模型连接配置目录；桌面 WebView 内所有选择器共享一次请求。 */
export function loadModelCatalog(options: { force?: boolean } = {}): Promise<ModelCatalog | null> {
  if (!options.force && snapshot.status === "ready") return Promise.resolve(snapshot.catalog);
  if (!options.force && activeRequest) return activeRequest;

  activeController?.abort();
  const controller = new AbortController();
  const generation = ++requestGeneration;
  activeController = controller;
  setSnapshot({ status: "loading", catalog: snapshot.catalog, error: null });

  const request = getModelConfigs({ signal: controller.signal })
    .then((configs) => {
      if (controller.signal.aborted || generation !== requestGeneration) return null;
      const nextCatalog = buildModelCatalog(configs);
      setSnapshot({ status: "ready", catalog: nextCatalog, error: null });
      return nextCatalog;
    })
    .catch((error: unknown) => {
      if (controller.signal.aborted || generation !== requestGeneration || isAbortError(error)) return null;
      const nextError = error instanceof Error ? error : new Error("模型配置加载失败");
      setSnapshot({ status: "error", catalog: snapshot.catalog, error: nextError });
      return null;
    })
    .finally(() => {
      if (generation === requestGeneration) {
        activeRequest = null;
        activeController = null;
      }
    });
  activeRequest = request;
  return request;
}

export function useModelCatalog(): ModelCatalogSnapshot & { retry: () => Promise<ModelCatalog | null> } {
  const current = useSyncExternalStore(
    subscribeModelCatalog,
    getModelCatalogSnapshot,
    getModelCatalogSnapshot,
  );
  useEffect(() => {
    if (current.status === "idle") void loadModelCatalog();
  }, [current.status]);
  return { ...current, retry: () => loadModelCatalog({ force: true }) };
}

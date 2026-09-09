import { useEffect, useSyncExternalStore } from "react";

import {
  getModelGroups,
  type ModelListItem,
  type ProviderModelGroup,
} from "@/lib/api/models";

export type ModelCatalogModel = {
  optionId: string;
  providerId: number;
  providerName: string;
  modelName: string;
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

const initialSnapshot: ModelCatalogSnapshot = {
  status: "idle",
  catalog: null,
  error: null,
};

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

export function modelOptionId(providerId: number, modelName: string): string {
  return `provider:${providerId}:model:${encodeURIComponent(modelName)}`;
}

function mapModel(provider: ProviderModelGroup, model: ModelListItem): ModelCatalogModel {
  return {
    optionId: modelOptionId(provider.provider_id, model.model_name),
    providerId: provider.provider_id,
    providerName: provider.provider_name,
    modelName: model.model_name,
    supportsReasoningEffort: model.supports_reasoning_effort,
    supportsImage: model.supports_image,
    supportsVideo: model.supports_video,
  };
}

export function buildModelCatalog(groups: readonly ProviderModelGroup[]): ModelCatalog {
  const models = groups.flatMap((provider) =>
    provider.models.map((model) => mapModel(provider, model)),
  );
  return {
    models,
    byOptionId: new Map(models.map((model) => [model.optionId, model])),
  };
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

/**
 * 加载全局模型目录。
 *
 * 同一 WebView 内所有模型选择器共享这一份目录请求。强制刷新时会 abort
 * 旧请求；即使底层 fetch 无法及时中止，也会用 generation 丢弃过期响应，
 * 防止旧目录覆盖新目录。
 */
export function loadModelCatalog(options: { force?: boolean } = {}): Promise<ModelCatalog | null> {
  if (!options.force && snapshot.status === "ready") {
    return Promise.resolve(snapshot.catalog);
  }
  if (!options.force && activeRequest) return activeRequest;

  activeController?.abort();
  const controller = new AbortController();
  const generation = ++requestGeneration;
  activeController = controller;
  setSnapshot({ status: "loading", catalog: snapshot.catalog, error: null });

  const request = getModelGroups({ signal: controller.signal })
    .then((groups) => {
      if (controller.signal.aborted || generation !== requestGeneration) return null;
      const nextCatalog = buildModelCatalog(groups);
      setSnapshot({ status: "ready", catalog: nextCatalog, error: null });
      return nextCatalog;
    })
    .catch((error: unknown) => {
      if (controller.signal.aborted || generation !== requestGeneration || isAbortError(error)) {
        return null;
      }
      const nextError = error instanceof Error ? error : new Error("模型目录加载失败");
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

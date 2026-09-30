import { postJson, requestJson, sendJson } from "@/lib/http/client";

export type ModelConfig = {
  config_id: number;
  config_name: string;
  base_url: string;
  api_key: string;
  model_name: string;
  context_window_k: number;
  api_key_configured: boolean;
  enabled: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
  supports_thinking: boolean;
  supports_image: boolean;
  supports_reasoning_effort: boolean;
};

export type ModelConfigCreateInput = {
  config_name: string;
  base_url: string;
  api_key: string;
  model_name: string;
  context_window_k: number;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  supports_image: boolean;
  sort_order?: number;
};

export type ModelConfigUpdateInput = Partial<Omit<ModelConfigCreateInput, "api_key">> & {
  api_key?: string;
  enabled?: boolean;
};

export type ModelConfigTestInput = Omit<ModelConfigCreateInput, "sort_order">;

export type ModelConfigTestResult = {
  config_id: number | null;
  success: boolean;
  elapsed_ms: number | null;
  error_code?: string | null;
  error_message?: string | null;
};

export type ModelConfigDiscoveryInput = {
  base_url: string;
  api_key: string;
};

export type ModelConfigDiscoveryResponse = {
  models: string[];
};

export const getModelConfigs = (init?: RequestInit) =>
  requestJson<ModelConfig[]>("/model-configs", init);

export const createModelConfig = (input: ModelConfigCreateInput) =>
  postJson<ModelConfig>("/model-configs", input);

export const updateModelConfig = (configId: number, input: ModelConfigUpdateInput) =>
  sendJson<ModelConfig>(`/model-configs/${configId}`, "PUT", input);

export const deleteModelConfig = (configId: number) =>
  requestJson<{ config_id: number; deleted: boolean }>(`/model-configs/${configId}`, {
    method: "DELETE",
  });

export const testDraftModelConfig = (input: ModelConfigTestInput) =>
  postJson<ModelConfigTestResult>("/model-configs/test", input);

export const discoverModelConfigs = (
  input: ModelConfigDiscoveryInput,
  init?: RequestInit,
) => postJson<ModelConfigDiscoveryResponse>("/model-configs/discover", input, init);

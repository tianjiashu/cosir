import { requestJson, type HttpRequestInit } from "@/lib/http/client";

export type ModelListItem = {
  model_name: string;
  supports_thinking: boolean;
  supports_image: boolean;
  supports_video: boolean;
  supports_reasoning_effort: boolean;
};

export type ProviderModelGroup = {
  provider_id: number;
  provider_name: string;
  models: ModelListItem[];
};

export function getModelGroups(
  init?: HttpRequestInit,
): Promise<ProviderModelGroup[]> {
  return requestJson<ProviderModelGroup[]>("/models", init);
}

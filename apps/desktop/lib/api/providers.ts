import { requestJson } from "@/lib/http/client";

export type Provider = {
  provider_id: number;
  name: string;
  base_url: string | null;
  api_key_configured: boolean;
  enabled: boolean;
  model_count: number;
  sort_order: number;
  created_at: string;
  updated_at: string;
};

export type ProviderCatalogItem = {
  name: string;
  provider_type: string;
  default_base_url: string | null;
  requires_api_key: boolean;
  models: string[];
};

export type ProviderCreateInput = {
  name: string;
  model_name?: string;
  base_url?: string;
  api_key?: string;
  sort_order?: number;
};

export type ProviderUpdateInput = {
  base_url?: string;
  api_key?: string;
  enabled?: boolean;
  sort_order?: number;
};

export type ProviderTestResult = {
  provider_id: number;
  success: boolean;
  elapsed_ms: number | null;
  error_code?: string;
  error_message?: string;
};

export const getProviders = () => requestJson<Provider[]>("/providers");

export const getProviderCatalog = () =>
  requestJson<ProviderCatalogItem[]>("/providers/catalog");

export const createProvider = (input: ProviderCreateInput) =>
  requestJson<Provider>("/providers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });

export const updateProvider = (providerId: number, input: ProviderUpdateInput) =>
  requestJson<Provider>(`/providers/${providerId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });

export const deleteProvider = (providerId: number) =>
  requestJson<{ provider_id: number; deleted: boolean }>(
    `/providers/${providerId}`,
    { method: "DELETE" },
  );

export const testProvider = (providerId: number) =>
  requestJson<ProviderTestResult>(`/providers/${providerId}/test`, {
    method: "POST",
  });

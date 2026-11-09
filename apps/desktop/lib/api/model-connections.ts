import { postJson, requestJson, sendJson, type RequestOptions } from "@/lib/http/client";

/** 连接测试会真实请求模型接口（含首 token 等待），明显长于默认 JSON 超时。 */
const CONNECTION_TEST_TIMEOUT_MS = 120_000;

/** 一个模型的能力声明；能力全部由用户填写，不从模型名推断。 */
export type ModelCapabilities = {
  context_window_k: number;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  supports_image: boolean;
};

/** 新增或测试一个模型时提交的字段。 */
export type ModelConfigFields = ModelCapabilities & { model_name: string };

/** 连接下的一个可选模型；`config_id`（「连接 id/模型名」）是 Run 与 Agent 配置引用它用的 id。 */
export type ModelConfig = ModelConfigFields & {
  config_id: string;
  connection_id: string;
};

/** 一个 OpenAI 兼容接口的访问凭据及其下模型；Key 只用于本机编辑表单回显。 */
export type ModelConnection = {
  connection_id: string;
  name: string;
  base_url: string;
  api_key: string;
  models: ModelConfig[];
};

export type ModelConnectionFields = {
  name: string;
  base_url: string;
  api_key: string;
};

export type ModelConfigTestResult = {
  success: boolean;
  elapsed_ms: number | null;
  error_code?: string | null;
  error_message?: string | null;
};

/** 模型配置文件：路径（设置页展示，供用户直接编辑）与全部连接；文件写坏时 400 并指出出错位置。 */
export type ModelConfigFile = {
  path: string;
  connections: ModelConnection[];
};

const ROOT = "/configuration/model-connections";
/** 连接 id 只含小写字母数字与 `-`；模型名可能带 `/`，后端用 path 参数接收，这里仍整体编码。 */
const modelPath = (connectionId: string, modelName: string) =>
  `${ROOT}/${connectionId}/models/${encodeURIComponent(modelName)}`;

export const getModelConfigFile = (options?: RequestOptions) =>
  requestJson<ModelConfigFile>(ROOT, options);

/** 新建连接与它的首批模型（至少一个模型）；返回写入后的连接。 */
export const createModelConnection = (input: ModelConnectionFields & { models: ModelConfigFields[] }) =>
  postJson<ModelConnection>(ROOT, input);

/** 只改传入的字段；不传 `api_key` 表示保留原 Key。 */
export const updateModelConnection = (connectionId: string, input: Partial<ModelConnectionFields>) =>
  sendJson<ModelConnection>(`${ROOT}/${connectionId}`, "PUT", input);

export const deleteModelConnection = (connectionId: string) =>
  requestJson<{ connection_id: string; deleted: boolean }>(`${ROOT}/${connectionId}`, { method: "DELETE" });

/** 给已有连接追加一批模型；已在该连接下的模型名整批被拒（409）。 */
export const addModelConfigs = (connectionId: string, models: ModelConfigFields[]) =>
  postJson<ModelConnection>(`${ROOT}/${connectionId}/models`, { models });

export const updateModelConfig = (connectionId: string, modelName: string, input: Partial<ModelCapabilities>) =>
  sendJson<ModelConfig>(modelPath(connectionId, modelName), "PUT", input);

export const deleteModelConfig = (connectionId: string, modelName: string) =>
  requestJson<{ config_id: string; deleted: boolean }>(modelPath(connectionId, modelName), { method: "DELETE" });

/** 按「这个连接 + 这个模型 + 这组能力」做一次真实探活，不落库。 */
export const testDraftModelConfig = (input: ModelConfigFields & Pick<ModelConnectionFields, "base_url" | "api_key">) =>
  postJson<ModelConfigTestResult>(`${ROOT}/test`, input, { timeoutMs: CONNECTION_TEST_TIMEOUT_MS });

/** 远端模型目录；远端失败时后端返回空列表。 */
export const discoverModels = (
  input: Pick<ModelConnectionFields, "base_url" | "api_key">,
  options?: RequestOptions,
) => postJson<{ models: string[] }>(`${ROOT}/discover`, input, options);

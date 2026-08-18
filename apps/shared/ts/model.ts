/**
 * 模型厂商域共享契约（Provider / Model）。
 *
 * 本文件为手写共享类型（非 `scripts/generate_api_ts.py` 生成物）：
 * 后端 providers/models 相关 schema 暂未纳入该生成器，故在此与
 * `apps/backend/app/api/schemas/` 的 request/response 模型人工对齐。
 * 字段命名沿用后端 snake_case（与 task.ts / turn.ts 一致）。
 *
 * 同时承载该域的 API 路径常量（MODEL_PROVIDER_PATHS），避免向生成物
 * api.ts 的 API_PATHS 手工追加（生成器重跑时会丢失）。
 *
 * @module shared/model
 */

/** 模型厂商域 API 路径常量（无 /api 前缀，与 API_PATHS 约定一致）。 */
export const MODEL_PROVIDER_PATHS = {
  PROVIDERS: "/providers",
  PROVIDER_DETAIL: (providerId: string) => `/providers/${providerId}`,
  PROVIDER_DISCOVER: (providerId: string) => `/providers/${providerId}/discover`,
  PROVIDER_MODELS: (providerId: string) => `/providers/${providerId}/models`,
  MODELS: "/models",
  MODEL_DETAIL: (modelId: string) => `/models/${modelId}`,
} as const;

/** 允许的厂商类型枚举（决定 litellm 前缀与默认 base_url，与后端 PROVIDER_TYPES 对齐）。 */
export type ProviderType =
  | "deepseek"
  | "openai-compatible"
  | "anthropic"
  | "ollama"
  | "custom";

/** 厂商配置记录（GET /providers 响应项，含聚合状态）。 */
export interface ProviderRecord {
  /** 厂商标识。 */
  provider_id: string;
  /** 厂商显示名（全局唯一）。 */
  name: string;
  /** 厂商类型。 */
  type: ProviderType;
  /** 自定义接入地址（可空，空时 litellm 内置解析）。 */
  base_url: string | null;
  /** Key 配置状态：不依赖 Key 的厂商类型恒为 true（如本地 Ollama）。 */
  api_key_configured: boolean;
  /** 启用开关（禁用厂商下的模型不进入下拉）。 */
  enabled: boolean;
  /** 该厂商下模型条目数（含禁用条目）。 */
  model_count: number;
  /** 排序权重。 */
  sort_order: number;
  /** 创建时间文本。 */
  created_at: string;
  /** 更新时间文本。 */
  updated_at: string;
}

/** 厂商创建请求体（POST /providers）。 */
export interface ProviderCreateRequest {
  name: string;
  type: ProviderType;
  base_url?: string | null;
  /** API Key 明文（DB 唯一事实来源；响应与日志不回传明文）。 */
  api_key?: string | null;
  enabled?: boolean;
  sort_order?: number;
}

/**
 * 厂商更新请求体（PUT /providers/{id}）。
 *
 * 仅覆盖显式传入的字段；置空 base_url 须显式传空字符串 ""；api_key 传 null
 * 表示不更新、传 "" 表示清除。
 */
export interface ProviderUpdateRequest {
  name?: string;
  type?: ProviderType;
  base_url?: string | null;
  /** API Key 明文；null 不更新，"" 清除。 */
  api_key?: string | null;
  enabled?: boolean;
  sort_order?: number;
}

/** 模型条目记录（GET /models 响应项，仅启用模型 + 启用厂商）。 */
export interface ModelEntryRecord {
  /** 模型条目标识。 */
  model_id: string;
  /** 归属厂商标识。 */
  provider_id: string;
  /** 归属厂商显示名（下拉分组展示用）。 */
  provider_name: string;
  /** litellm 路由名（带 provider 前缀，如 `deepseek/deepseek-v4-flash`）。 */
  model_name: string;
  /** 下拉展示名（可省略前缀）。 */
  display_name: string;
  /** 上下文窗口（token）。 */
  max_context_window: number;
  /** 推理模型标识。 */
  supports_thinking: boolean;
  /** 默认采样温度（可空）。 */
  temperature: number | null;
  /** 默认核采样参数（可空）。 */
  top_p: number | null;
  /** 默认最大输出 token 数（可空）。 */
  max_tokens: number | null;
  /** 启用开关。 */
  enabled: boolean;
  /** 归属厂商的 Key 配置状态（发送前校验依据）。 */
  api_key_configured: boolean;
  /** 组内排序权重。 */
  sort_order: number;
  /** 创建时间文本。 */
  created_at: string;
  /** 更新时间文本。 */
  updated_at: string;
}

/** 单条模型条目的创建/导入请求体（手动添加与批量导入的条目单元）。 */
export interface ModelCreateRequest {
  model_name: string;
  display_name: string;
  max_context_window: number;
  supports_thinking?: boolean;
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
  enabled?: boolean;
  sort_order?: number;
}

/** 按厂商批量导入模型条目请求体（POST /providers/{id}/models）。 */
export interface ModelBulkImportRequest {
  models: ModelCreateRequest[];
}

/** 模型条目更新请求体（PUT /models/{id}，仅覆盖显式传入字段）。 */
export interface ModelUpdateRequest {
  display_name?: string;
  max_context_window?: number;
  supports_thinking?: boolean;
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
  enabled?: boolean;
  sort_order?: number;
}

/** discover 候选模型（POST /providers/{id}/discover 响应项）。 */
export interface ModelCandidate {
  /** litellm 路由名（带 provider 前缀）。 */
  model_name: string;
  /** 去前缀后的展示名。 */
  display_name: string;
  /** litellm 已知的上下文窗口（token，预填可改）。 */
  max_context_window: number;
  /** litellm 目录标注的推理模型标识。 */
  supports_thinking: boolean;
  /** 该厂商下是否已存在同名条目（前端置灰依据）。 */
  already_imported: boolean;
}

/** 模型批量导入结果（POST /providers/{id}/models 响应）。 */
export interface ModelImportResult {
  /** 导入目标厂商标识。 */
  provider_id: string;
  /** 成功导入的条目列表（含厂商聚合信息）。 */
  imported: ModelEntryRecord[];
  /** 被跳过的模型名列表（该厂商下已存在同名条目）。 */
  skipped_model_names: string[];
}

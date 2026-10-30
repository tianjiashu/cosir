import type { ClaudeModelChoice } from "@/lib/api/claude-subscription";
import type { ModelConfig, ModelConfigTestInput, ModelProvider } from "@/lib/api/model-configs";

export type FormState = {
  provider: ModelProvider;
  configName: string;
  baseUrl: string;
  apiKey: string;
  modelName: string;
  contextWindowK: string;
  supportsThinking: boolean;
  supportsReasoningEffort: boolean;
  supportsImage: boolean;
};

export const emptyForm: FormState = {
  provider: "openai",
  configName: "",
  baseUrl: "",
  apiKey: "",
  modelName: "",
  contextWindowK: "128",
  supportsThinking: false,
  supportsReasoningEffort: false,
  supportsImage: false,
};

export function toForm(config: ModelConfig): FormState {
  return {
    provider: config.provider,
    configName: config.config_name,
    baseUrl: config.base_url,
    apiKey: config.api_key,
    modelName: config.model_name,
    contextWindowK: String(config.context_window_k),
    supportsThinking: config.supports_thinking,
    supportsReasoningEffort: config.supports_reasoning_effort,
    supportsImage: config.supports_image,
  };
}

export function toInput(form: FormState): ModelConfigTestInput {
  return {
    provider: form.provider,
    config_name: form.configName.trim(),
    base_url: form.baseUrl.trim(),
    api_key: form.apiKey.trim(),
    model_name: form.modelName.trim(),
    context_window_k: Number(form.contextWindowK),
    supports_thinking: form.supportsThinking,
    supports_reasoning_effort: form.supportsReasoningEffort,
    supports_image: form.supportsImage,
  };
}

/** 切换认证来源时清除旧连接和密钥，避免把 API Key 带入订阅配置。 */
export function changeProvider(form: FormState, provider: ModelProvider): FormState {
  return {
    ...emptyForm,
    configName: form.configName || (provider === "claude_subscription" ? "Claude 订阅" : ""),
    provider,
    ...(provider === "claude_subscription" ? {
      baseUrl: "https://api.anthropic.com",
      contextWindowK: "200",
      supportsThinking: true,
      supportsReasoningEffort: true,
    } : {}),
  };
}

/** 模型切换时一次性应用目录声明；后续用户调整不会被目录刷新覆盖。 */
export function selectClaudeModel(form: FormState, model: ClaudeModelChoice): FormState {
  return {
    ...form,
    modelName: model.id,
    contextWindowK: String(model.context_window_k),
    supportsThinking: model.supports_thinking,
    supportsReasoningEffort: model.supports_reasoning_effort,
    supportsImage: model.supports_image,
  };
}

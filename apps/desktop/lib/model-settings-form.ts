export type TriStateValue = "" | "true" | "false";

/**
 * Agent 模型覆盖项的结构化表单值。
 *
 * 空字符串表示不覆盖运行时默认值；保存时会被转换为缺省字段，而不是写入空值。
 */
export type ModelSettingsForm = {
  temperature: string;
  top_p: string;
  max_tokens: string;
  drop_params: TriStateValue;
  stream: TriStateValue;
  reasoning_effort: string;
  response_format: string;
};

/** 返回一份不覆盖任何模型参数的空表单。 */
export function emptyModelSettingsForm(): ModelSettingsForm {
  return {
    temperature: "",
    top_p: "",
    max_tokens: "",
    drop_params: "",
    stream: "",
    reasoning_effort: "",
    response_format: "",
  };
}

function stringValue(value: unknown): string {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean"
    ? String(value)
    : "";
}

/**
 * 将后端的 JSON 模型覆盖项转换为页面表单值。
 *
 * 该函数只负责边界转换，不修改输入对象，也不负责判断模型供应商是否支持某个参数。
 */
export function modelSettingsToForm(settings: Record<string, unknown> | null | undefined): ModelSettingsForm {
  const source = settings && typeof settings === "object" ? settings : {};
  return {
    temperature: stringValue(source.temperature),
    top_p: stringValue(source.top_p),
    max_tokens: stringValue(source.max_tokens),
    drop_params: stringValue(source.drop_params) as TriStateValue,
    stream: stringValue(source.stream) as TriStateValue,
    reasoning_effort: stringValue(source.reasoning_effort),
    response_format: stringValue(source.response_format),
  };
}

function parseNumber(value: string, field: string): number | undefined {
  const normalized = value.trim();
  if (!normalized) return undefined;
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed)) throw new Error(`${field} 必须是有限数字`);
  return parsed;
}

function parseBoolean(value: TriStateValue): boolean | undefined {
  if (value === "") return undefined;
  return value === "true";
}

/**
 * 将页面结构化表单转换为后端持久化的 model_settings JSON 对象。
 *
 * 数字字段在这里完成类型转换和基本范围校验；空字段表示不覆盖，布尔字段保留三态语义。
 * 该函数不写文件、不发请求，错误由表单保存流程展示给用户。
 */
export function modelSettingsFromForm(form: ModelSettingsForm): Record<string, unknown> {
  const temperature = parseNumber(form.temperature, "temperature");
  const topP = parseNumber(form.top_p, "top_p");
  const maxTokens = parseNumber(form.max_tokens, "max_tokens");
  if (temperature !== undefined && temperature < 0) throw new Error("temperature 不能小于 0");
  if (topP !== undefined && (topP < 0 || topP > 1)) throw new Error("top_p 必须介于 0 和 1 之间");
  if (maxTokens !== undefined && (!Number.isInteger(maxTokens) || maxTokens <= 0)) {
    throw new Error("max_tokens 必须是正整数");
  }

  const settings: Record<string, unknown> = {};
  if (temperature !== undefined) settings.temperature = temperature;
  if (topP !== undefined) settings.top_p = topP;
  if (maxTokens !== undefined) settings.max_tokens = maxTokens;

  const dropParams = parseBoolean(form.drop_params);
  const stream = parseBoolean(form.stream);
  if (dropParams !== undefined) settings.drop_params = dropParams;
  if (stream !== undefined) settings.stream = stream;
  if (form.reasoning_effort.trim()) settings.reasoning_effort = form.reasoning_effort.trim();
  if (form.response_format.trim()) settings.response_format = form.response_format.trim();
  return settings;
}

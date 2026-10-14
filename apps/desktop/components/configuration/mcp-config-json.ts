import type { MCPServerConfiguration, MCPTransport } from "@/lib/api/system-extensions";

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readStringMap(value: unknown, field: string): Record<string, string> {
  if (value === undefined) return {};
  if (!isRecord(value) || Object.values(value).some((item) => typeof item !== "string")) {
    throw new Error(`${field} 必须是字符串键值对象`);
  }
  return value as Record<string, string>;
}

/** 把单个服务转换为常见的 mcpServers JSON 配置片段。 */
export function formatMcpServerJson(server: MCPServerConfiguration): string {
  const config = server.transport === "stdio"
    ? { command: server.command, args: server.args, ...(Object.keys(server.env).length ? { env: server.env } : {}) }
    : {
        type: server.transport === "sse" ? "sse" : "http",
        url: server.url,
        ...(Object.keys(server.headers).length ? { headers: server.headers } : {}),
      };
  return JSON.stringify(
    { mcpServers: { [server.name]: { ...config, ...(server.enabled ? {} : { enabled: false }) } } },
    null,
    2,
  );
}

/** 解析单服务 mcpServers 草稿；必填项留给保存或测试边界校验，保证未完成草稿可切换编辑方式。 */
export function parseMcpServerJson(text: string): MCPServerConfiguration {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("JSON 格式无效，请检查逗号、引号和括号");
  }

  if (!isRecord(parsed) || !isRecord(parsed.mcpServers)) {
    throw new Error("配置需要包含 mcpServers 对象");
  }
  if (Object.keys(parsed).some((field) => field !== "mcpServers")) {
    throw new Error("当前仅支持包含 mcpServers 的配置对象");
  }
  const entries = Object.entries(parsed.mcpServers);
  if (entries.length !== 1) {
    throw new Error("每次只能配置一个 MCP 服务，请在 mcpServers 中保留一个服务项");
  }

  const [name, rawConfig] = entries[0];
  if (!isRecord(rawConfig)) throw new Error("服务配置必须是对象");

  const allowedFields = new Set(["command", "args", "env", "type", "url", "headers", "enabled"]);
  const unsupportedField = Object.keys(rawConfig).find((field) => !allowedFields.has(field));
  if (unsupportedField) {
    throw new Error(`当前暂不支持服务配置字段：${unsupportedField}`);
  }

  const enabled = rawConfig.enabled ?? true;
  if (typeof enabled !== "boolean") throw new Error("enabled 必须是布尔值");

  let transport: MCPTransport;
  let command = "";
  let args: string[] = [];
  let env: Record<string, string> = {};
  let url = "";
  let headers: Record<string, string> = {};

  if (typeof rawConfig.command === "string") {
    if (rawConfig.type !== undefined && rawConfig.type !== "stdio") {
      throw new Error("stdio 配置的 type 只能是 stdio");
    }
    if (rawConfig.args !== undefined && (!Array.isArray(rawConfig.args) || rawConfig.args.some((item) => typeof item !== "string"))) {
      throw new Error("args 必须是字符串数组");
    }
    transport = "stdio";
    command = rawConfig.command;
    args = (rawConfig.args as string[] | undefined) ?? [];
    env = readStringMap(rawConfig.env, "env");
  } else {
    if (rawConfig.type !== "http" && rawConfig.type !== "sse") {
      throw new Error("HTTP 服务的 type 必须是 http 或 sse");
    }
    if (rawConfig.url !== undefined && typeof rawConfig.url !== "string") {
      throw new Error("HTTP 服务的 url 必须是字符串");
    }
    transport = rawConfig.type === "sse" ? "sse" : "streamable_http";
    url = (rawConfig.url as string | undefined) ?? "";
    headers = readStringMap(rawConfig.headers, "headers");
  }

  return { name: name.trim(), transport, command, args, env, url, headers, enabled };
}

/** 在保存或连接测试边界校验草稿的必填连接参数。 */
export function validateMcpServerConfiguration(server: MCPServerConfiguration): void {
  if (!server.name.trim() || (server.transport === "stdio" ? !server.command.trim() : !server.url.trim())) {
    throw new Error(server.transport === "stdio" ? "请填写服务名称和启动命令" : "请填写服务名称和 HTTP endpoint");
  }
}

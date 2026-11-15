import type { MCPOAuthConfiguration, MCPServerConfiguration, MCPTransport } from "@/lib/api/extensions";
import { UserFacingError } from "@/lib/errors/user-facing-error";

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readStringMap(value: unknown, field: string): Record<string, string> {
  if (value === undefined) return {};
  if (!isRecord(value) || Object.values(value).some((item) => typeof item !== "string")) {
    throw new UserFacingError(`${field} 必须是字符串键值对象`);
  }
  return value as Record<string, string>;
}

/**
 * `oauth` 块在 JSON 里用 Claude Code `.mcp.json` 的写法（`clientId`、`clientSecret`、`callbackPort`、`scope`），
 * 从那边复制来的配置不用改；落盘与接口仍是 `client_id` 等写法，只在这里转换。
 */
function formatOAuth(oauth: MCPOAuthConfiguration): Record<string, unknown> {
  return {
    ...(oauth.client_id ? { clientId: oauth.client_id } : {}),
    ...(oauth.client_secret ? { clientSecret: oauth.client_secret } : {}),
    ...(oauth.callback_port !== null ? { callbackPort: oauth.callback_port } : {}),
    ...(oauth.scope ? { scope: oauth.scope } : {}),
  };
}

function parseOAuth(value: unknown): MCPOAuthConfiguration | null {
  if (value === undefined) return null;
  if (!isRecord(value)) throw new UserFacingError("oauth 必须是对象");
  const unsupported = Object.keys(value).find((field) => !["clientId", "clientSecret", "callbackPort", "scope"].includes(field));
  if (unsupported) throw new UserFacingError(`当前暂不支持 oauth 字段：${unsupported}`);
  const text = (field: string): string => {
    const item = value[field];
    if (item !== undefined && typeof item !== "string") throw new UserFacingError(`oauth.${field} 必须是字符串`);
    return (item as string | undefined) ?? "";
  };
  const port = value.callbackPort;
  if (port !== undefined && (typeof port !== "number" || !Number.isInteger(port))) {
    throw new UserFacingError("oauth.callbackPort 必须是整数");
  }
  return { client_id: text("clientId"), client_secret: text("clientSecret"), callback_port: (port as number | undefined) ?? null, scope: text("scope") };
}

/** 把单个服务转换为常见的 mcpServers JSON 配置片段。 */
export function formatMcpServerJson(server: MCPServerConfiguration): string {
  const config = server.transport === "stdio"
    ? { command: server.command, args: server.args, ...(Object.keys(server.env).length ? { env: server.env } : {}) }
    : {
        type: server.transport === "sse" ? "sse" : "http",
        url: server.url,
        ...(Object.keys(server.headers).length ? { headers: server.headers } : {}),
        ...(server.oauth ? { oauth: formatOAuth(server.oauth) } : {}),
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
    throw new UserFacingError("JSON 格式无效，请检查逗号、引号和括号");
  }

  if (!isRecord(parsed) || !isRecord(parsed.mcpServers)) {
    throw new UserFacingError("配置需要包含 mcpServers 对象");
  }
  if (Object.keys(parsed).some((field) => field !== "mcpServers")) {
    throw new UserFacingError("当前仅支持包含 mcpServers 的配置对象");
  }
  const entries = Object.entries(parsed.mcpServers);
  if (entries.length !== 1) {
    throw new UserFacingError("每次只能配置一个 MCP 服务，请在 mcpServers 中保留一个服务项");
  }

  const [name, rawConfig] = entries[0];
  if (!isRecord(rawConfig)) throw new UserFacingError("服务配置必须是对象");

  const allowedFields = new Set(["command", "args", "env", "type", "url", "headers", "oauth", "enabled"]);
  const unsupportedField = Object.keys(rawConfig).find((field) => !allowedFields.has(field));
  if (unsupportedField) {
    throw new UserFacingError(`当前暂不支持服务配置字段：${unsupportedField}`);
  }

  const enabled = rawConfig.enabled ?? true;
  if (typeof enabled !== "boolean") throw new UserFacingError("enabled 必须是布尔值");

  let transport: MCPTransport;
  let command = "";
  let args: string[] = [];
  let env: Record<string, string> = {};
  let url = "";
  let headers: Record<string, string> = {};
  let oauth: MCPOAuthConfiguration | null = null;

  if (typeof rawConfig.command === "string") {
    if (rawConfig.type !== undefined && rawConfig.type !== "stdio") {
      throw new UserFacingError("stdio 配置的 type 只能是 stdio");
    }
    if (rawConfig.args !== undefined && (!Array.isArray(rawConfig.args) || rawConfig.args.some((item) => typeof item !== "string"))) {
      throw new UserFacingError("args 必须是字符串数组");
    }
    transport = "stdio";
    command = rawConfig.command;
    args = (rawConfig.args as string[] | undefined) ?? [];
    env = readStringMap(rawConfig.env, "env");
    if (rawConfig.oauth !== undefined) throw new UserFacingError("stdio 服务不使用 oauth");
  } else {
    if (rawConfig.type !== "http" && rawConfig.type !== "sse") {
      throw new UserFacingError("HTTP 服务的 type 必须是 http 或 sse");
    }
    if (rawConfig.url !== undefined && typeof rawConfig.url !== "string") {
      throw new UserFacingError("HTTP 服务的 url 必须是字符串");
    }
    transport = rawConfig.type === "sse" ? "sse" : "streamable_http";
    url = (rawConfig.url as string | undefined) ?? "";
    headers = readStringMap(rawConfig.headers, "headers");
    oauth = parseOAuth(rawConfig.oauth);
  }

  return { name: name.trim(), transport, command, args, env, url, headers, oauth, enabled };
}

/** 在保存或连接测试边界校验草稿的必填连接参数。 */
export function validateMcpServerConfiguration(server: MCPServerConfiguration): void {
  if (!server.name.trim() || (server.transport === "stdio" ? !server.command.trim() : !server.url.trim())) {
    throw new UserFacingError(server.transport === "stdio" ? "请填写服务名称和启动命令" : "请填写服务名称和 HTTP endpoint");
  }
  const port = server.oauth?.callback_port;
  if (port !== undefined && port !== null && !(Number.isInteger(port) && port >= 1024 && port <= 65535)) {
    throw new UserFacingError("OAuth 回调端口必须是 1024–65535 之间的整数");
  }
}

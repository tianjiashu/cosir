import { jsonRequestInit, requestJson, type RequestOptions } from "@/lib/http/client";

/** MCP 连接测试需要启动服务进程并握手，Skill 导入需要复制 / 解压目录，均明显长于默认 JSON 超时。 */
const LONG_OPERATION_TIMEOUT_MS = 120_000;

export type MCPTransport = "stdio" | "sse" | "streamable_http";

export type MCPServerConfiguration = {
  name: string;
  transport: MCPTransport;
  command: string;
  args: string[];
  env: Record<string, string>;
  url: string;
  headers: Record<string, string>;
  enabled: boolean;
  tested?: boolean;
};

export type MCPDiscoveredTool = {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
};

export type MCPServerTestResult = {
  success: boolean;
  tools: MCPDiscoveredTool[];
  error: string | null;
  agent_available: boolean;
};

export type SystemSkill = {
  name: string;
  description: string;
  enabled: boolean;
};

export type SystemSkillDocument = SystemSkill & { content: string };

export function getMCPServers(options?: RequestOptions): Promise<{ servers: MCPServerConfiguration[] }> {
  return requestJson("/configuration/mcp-servers", options);
}

function mcpServerPayload(server: MCPServerConfiguration) {
  return {
    name: server.name,
    transport: server.transport,
    command: server.command,
    args: server.args,
    env: server.env,
    url: server.url,
    headers: server.headers,
    enabled: server.enabled,
  };
}

export function saveMCPServers(servers: MCPServerConfiguration[]): Promise<{ servers: MCPServerConfiguration[] }> {
  return requestJson(
    "/configuration/mcp-servers",
    jsonRequestInit({ servers: servers.map(mcpServerPayload) }, { method: "PUT" }),
  );
}

export function testMCPServer(server: MCPServerConfiguration): Promise<MCPServerTestResult> {
  return requestJson(
    "/configuration/mcp-servers/test",
    jsonRequestInit(mcpServerPayload(server), { method: "POST", timeoutMs: LONG_OPERATION_TIMEOUT_MS }),
  );
}

export function getSystemSkills(options?: RequestOptions): Promise<SystemSkill[]> {
  return requestJson("/configuration/skills", options);
}

export function getSystemSkill(name: string, options?: RequestOptions): Promise<SystemSkillDocument> {
  return requestJson(`/configuration/skills/${encodeURIComponent(name)}`, options);
}

export function saveEnabledSkills(enabled_skills: string[]): Promise<SystemSkill[]> {
  return requestJson(
    "/configuration/skills/enabled",
    jsonRequestInit({ enabled_skills }, { method: "PUT" }),
  );
}

export function importSystemSkill(
  source_path: string,
  source_type: "directory" | "archive",
): Promise<SystemSkill> {
  return requestJson(
    "/configuration/skills/import",
    jsonRequestInit({ source_path, source_type }, { method: "POST", timeoutMs: LONG_OPERATION_TIMEOUT_MS }),
  );
}

export function deleteSystemSkill(name: string): Promise<{ name: string; deleted: boolean }> {
  return requestJson(`/configuration/skills/${encodeURIComponent(name)}`, { method: "DELETE" });
}

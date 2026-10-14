import { jsonRequestInit, requestJson } from "@/lib/http/client";

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

export function getMCPServers(): Promise<{ servers: MCPServerConfiguration[] }> {
  return requestJson("/configuration/mcp-servers");
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
    jsonRequestInit(mcpServerPayload(server), { method: "POST" }),
  );
}

export function getSystemSkills(): Promise<SystemSkill[]> {
  return requestJson("/configuration/skills");
}

export function getSystemSkill(name: string): Promise<SystemSkillDocument> {
  return requestJson(`/configuration/skills/${encodeURIComponent(name)}`);
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
    jsonRequestInit({ source_path, source_type }, { method: "POST" }),
  );
}

export function deleteSystemSkill(name: string): Promise<{ name: string; deleted: boolean }> {
  return requestJson(`/configuration/skills/${encodeURIComponent(name)}`, { method: "DELETE" });
}

import { jsonRequestInit, requestJson, type RequestOptions } from "@/lib/http/client";

/** MCP 连接测试需要启动服务进程并握手，Skill 导入需要复制 / 解压目录，均明显长于默认 JSON 超时。 */
const LONG_OPERATION_TIMEOUT_MS = 120_000;

/**
 * Skill 与 MCP 配置的作用域。工作区看到的是两层合并结果：同名时工作区覆盖系统级；
 * 在工作区里，`scope === "system"` 的条目是继承来的，只读。
 */
export type ExtensionScope = "system" | "workspace";

export type MCPTransport = "stdio" | "sse" | "streamable_http";

/** 一个 MCP 服务的可编辑连接配置（保存与测试的请求体）。 */
export type MCPServerConfiguration = {
  name: string;
  transport: MCPTransport;
  command: string;
  args: string[];
  env: Record<string, string>;
  url: string;
  headers: Record<string, string>;
  enabled: boolean;
};

/** 配置页列出的一个可见服务：`tested` 取自本机测试记录。 */
export type MCPServer = MCPServerConfiguration & {
  tested: boolean;
  scope: ExtensionScope;
  overrides: boolean;
};

type MCPDiscoveredTool = {
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

export type Skill = {
  name: string;
  description: string;
  enabled: boolean;
  scope: ExtensionScope;
  overrides: boolean;
};

export type SkillDocument = Skill & { content: string };

/** `null` 为系统级配置；否则为该工作区的配置（列表含继承来的系统级条目）。 */
export type ExtensionScopeId = number | null;

function extensionPath(workspaceId: ExtensionScopeId, path: string): string {
  return workspaceId === null ? `/configuration${path}` : `/workspaces/${workspaceId}/configuration${path}`;
}

/** 写操作只作用于请求所在的作用域。 */
export function ownScope(workspaceId: ExtensionScopeId): ExtensionScope {
  return workspaceId === null ? "system" : "workspace";
}

export function getMCPServers(workspaceId: ExtensionScopeId, options?: RequestOptions): Promise<{ servers: MCPServer[] }> {
  return requestJson(extensionPath(workspaceId, "/mcp-servers"), options);
}

/** 只取可编辑字段：保存与测试的请求体里不能带 `tested` / `scope` 等展示字段。 */
export function mcpServerConfiguration(server: MCPServerConfiguration): MCPServerConfiguration {
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

/** 整表替换本作用域自己的服务；调用方只传本作用域的条目，不能混入继承来的系统级服务。 */
export function saveMCPServers(workspaceId: ExtensionScopeId, servers: MCPServerConfiguration[]): Promise<{ servers: MCPServer[] }> {
  return requestJson(
    extensionPath(workspaceId, "/mcp-servers"),
    jsonRequestInit({ servers: servers.map(mcpServerConfiguration) }, { method: "PUT" }),
  );
}

export function testMCPServer(workspaceId: ExtensionScopeId, server: MCPServerConfiguration): Promise<MCPServerTestResult> {
  return requestJson(
    extensionPath(workspaceId, "/mcp-servers/test"),
    jsonRequestInit(mcpServerConfiguration(server), { method: "POST", timeoutMs: LONG_OPERATION_TIMEOUT_MS }),
  );
}

export function getSkills(workspaceId: ExtensionScopeId, options?: RequestOptions): Promise<Skill[]> {
  return requestJson(extensionPath(workspaceId, "/skills"), options);
}

export function getSkill(workspaceId: ExtensionScopeId, name: string, options?: RequestOptions): Promise<SkillDocument> {
  return requestJson(extensionPath(workspaceId, `/skills/${encodeURIComponent(name)}`), options);
}

export function setSkillEnabled(workspaceId: ExtensionScopeId, name: string, enabled: boolean): Promise<Skill> {
  return requestJson(
    extensionPath(workspaceId, `/skills/${encodeURIComponent(name)}/enabled`),
    jsonRequestInit({ enabled }, { method: "PUT" }),
  );
}

export function importSkill(
  workspaceId: ExtensionScopeId,
  source_path: string,
  source_type: "directory" | "archive",
): Promise<Skill> {
  return requestJson(
    extensionPath(workspaceId, "/skills/import"),
    jsonRequestInit({ source_path, source_type }, { method: "POST", timeoutMs: LONG_OPERATION_TIMEOUT_MS }),
  );
}

export function deleteSkill(workspaceId: ExtensionScopeId, name: string): Promise<{ name: string; deleted: boolean }> {
  return requestJson(extensionPath(workspaceId, `/skills/${encodeURIComponent(name)}`), { method: "DELETE" });
}

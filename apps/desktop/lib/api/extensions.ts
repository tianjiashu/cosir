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

/**
 * Skill 目录的身份：`cosir` 专属（只有 cosir 读，导入落在这里）或 `shared` 共享（`.agents/skills`，Codex 等也读）。
 * 对 Agent 两者平等；身份只回答「还有谁能看到」与「导入落在哪」。
 */
export type SkillRootKind = "cosir" | "shared";

/**
 * 一个可见 Skill。`overrides`：它遮住了优先级更低、内容不同的同名 Skill；`warnings`：不合标准但不妨碍使用；
 * `manual_only`：只供用户手动选用，不进模型看到的目录；`has_scripts`：带 `scripts/` 目录。
 */
export type Skill = {
  name: string;
  description: string;
  enabled: boolean;
  scope: ExtensionScope;
  kind: SkillRootKind;
  overrides: boolean;
  warnings: string[];
  manual_only: boolean;
  has_scripts: boolean;
};

/**
 * 配置页分组里的一个 Skill。`standing`：`effective` 是 Agent 实际用的那一条；`shadowed` 是被优先级更高、
 * 内容不同的同名 Skill 覆盖（内容相同的同一份不单列，见详情的 `also_at`）。停用名单按名称记录，所以只有
 * `effective` 的条目能启停。
 */
export type SkillEntry = Skill & {
  standing: "effective" | "shadowed";
};

/**
 * 一个根目录（作用域 × 身份）。目录还不存在时也列出（空）；`location` 给人看（系统级 `~/…`，工作区写成
 * 相对路径）；`skipped` 是无法加载的子目录及原因。后端按优先级从高到低排好（本层在前，同层专属在前）。
 */
export type SkillGroup = {
  scope: ExtensionScope;
  kind: SkillRootKind;
  location: string;
  skipped: { directory: string; reason: string }[];
  skills: SkillEntry[];
};

/**
 * 配置页的分组目录（后端按合并规则算好，前端不再推断谁生效）。分组按优先级从高到低；
 * `agent_skills` / `agent_tokens` 是 Agent 实际看到的目录条数与估算 token。
 */
export type SkillCatalogView = {
  agent_skills: number;
  agent_tokens: number;
  groups: SkillGroup[];
};

/**
 * Skill 详情：正文（已去掉 frontmatter）、其余 frontmatter 字段原样转文本、目录里的文件清单（限量，
 * `files_truncated` 表示没列全）。`location` 给人看；`also_at` 是同一份 Skill 的其他位置（软链或复制安装）；
 * `directory` 是绝对路径，只给「在文件夹中显示」。
 */
export type SkillDetail = Skill & {
  content: string;
  location: string;
  also_at: string[];
  directory: string;
  metadata: Record<string, string>;
  files: { path: string; size: number; binary: boolean }[];
  files_truncated: boolean;
};

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

export function getSkill(workspaceId: ExtensionScopeId, name: string, options?: RequestOptions): Promise<SkillDetail> {
  return requestJson(extensionPath(workspaceId, `/skills/${encodeURIComponent(name)}`), options);
}

/** Skill 目录内一个文本文件的只读预览；`path` 相对 Skill 目录。 */
export function getSkillFile(
  workspaceId: ExtensionScopeId,
  name: string,
  path: string,
  options?: RequestOptions,
): Promise<{ path: string; content: string }> {
  const query = new URLSearchParams({ path });
  return requestJson(extensionPath(workspaceId, `/skills/${encodeURIComponent(name)}/file?${query}`), options);
}

export function getSkillCatalog(workspaceId: ExtensionScopeId, options?: RequestOptions): Promise<SkillCatalogView> {
  return requestJson(extensionPath(workspaceId, "/skill-catalog"), options);
}

/** 一次启用或停用若干个本作用域的生效 Skill；含继承来的条目时后端整体拒绝，只写一次配置。 */
export function setSkillsEnabled(workspaceId: ExtensionScopeId, names: string[], enabled: boolean): Promise<Record<string, never>> {
  return requestJson(
    extensionPath(workspaceId, "/skills/enabled"),
    jsonRequestInit({ names, enabled }, { method: "PUT" }),
  );
}

/** 导入冲突时用户的选择：`replace` 替换专属目录里的同名 Skill，`override` 导入并覆盖别处的同名 Skill。 */
export type SkillImportResolution = "replace" | "override";

/**
 * 导入与现有同名 Skill 的冲突，随 409 `skill_import_conflict` 返回。`relation` 为 `identical` 时已有内容相同的
 * 生效条目，不导入；其余带上同名的 `resolution` 再提交一次（后端会重新判定）。`existing` 按优先级从高到低。
 */
export type SkillImportConflict = {
  name: string;
  relation: "identical" | SkillImportResolution;
  description: string;
  existing: { scope: ExtensionScope; kind: SkillRootKind; location: string; description: string; same_content: boolean }[];
};

export function importSkill(
  workspaceId: ExtensionScopeId,
  source_path: string,
  source_type: "directory" | "archive",
  resolution?: SkillImportResolution,
): Promise<Skill> {
  return requestJson(
    extensionPath(workspaceId, "/skills/import"),
    jsonRequestInit({ source_path, source_type, resolution }, { method: "POST", timeoutMs: LONG_OPERATION_TIMEOUT_MS }),
  );
}

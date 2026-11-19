import { configurationApiPath, type ConfigurationScope } from "@/lib/api/configuration-scope";
import { jsonRequestInit, requestJson, type RequestOptions } from "@/lib/http/client";

/** MCP 连接测试需要启动服务进程并握手，Skill 导入需要复制 / 解压目录，均明显长于默认 JSON 超时。 */
const LONG_OPERATION_TIMEOUT_MS = 120_000;

export type MCPTransport = "stdio" | "sse" | "streamable_http";

/**
 * 远程服务的 OAuth 参数（整块可为 `null`：自动注册客户端、回调端口随机）。服务不支持自动注册时填预先注册的
 * `client_id`，回调地址为 `http://127.0.0.1:<callback_port>/callback`；`client_secret` 只能写成 `${变量名}`。
 */
export type MCPOAuthConfiguration = {
  client_id: string;
  client_secret: string;
  callback_port: number | null;
  scope: string;
};

/** 一个 MCP 服务的可编辑连接配置（保存与测试的请求体）；值里可写 `${变量名}` 引用本机环境变量。 */
export type MCPServerConfiguration = {
  name: string;
  transport: MCPTransport;
  command: string;
  args: string[];
  env: Record<string, string>;
  url: string;
  headers: Record<string, string>;
  oauth: MCPOAuthConfiguration | null;
  enabled: boolean;
};

/**
 * 服务配置引用的一个环境变量（`${NAME}`）的值从哪来，不带值：`cosir` 在 cosir 的系统 `.env` 里（配置页可改），
 * `shell` 来自用户的 shell 环境（优先于 `.env`，配置页改不动）；`has_default` 表示没设置时有默认值兜底。
 */
export type MCPVariableStatus = {
  name: string;
  source: "unset" | "cosir" | "shell";
  has_default: boolean;
};

/** 后端由本机事实推出的主状态（规则只在后端 `configuration_view`）：停用 > 缺变量 > 配置有误 > 需要授权 > 待测试 > 可用。 */
export type MCPServerStatus =
  | "disabled"
  | "missing_variables"
  | "invalid_configuration"
  | "authorization_required"
  | "untested"
  | "available";

/**
 * 配置页列出的一个可见服务：`variables` 是它引用的环境变量；`literal_fields` 是工作区服务里直接写了值的
 * env / headers 字段（如 `env.API_KEY`），用于提醒改成变量。
 */
export type MCPServer = MCPServerConfiguration & {
  status: MCPServerStatus;
  scope: ConfigurationScope;
  overrides: boolean;
  variables: MCPVariableStatus[];
  literal_fields: string[];
};

type MCPDiscoveredTool = {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
};

/**
 * 一次连接测试（或登录授权）的结果：成功时带发现的工具，失败时带归类后的原因与 `failure_kind`。
 * `needs_sign_in` 是后端失败归类给出的下一步「登录授权」，前端只读它，不按 `failure_kind` 自行判断。
 */
export type MCPServerTestResult = {
  status: "completed";
  success: boolean;
  tools: MCPDiscoveredTool[];
  error: string | null;
  failure_kind: string | null;
  needs_sign_in: boolean;
  agent_available: boolean;
};

/** 登录授权停在浏览器这一步：打开 `authorization_url`，再按 `flow_id` 等最终结果。 */
export type MCPAuthorizationPending = {
  status: "authorization_required";
  flow_id: string;
  authorization_url: string;
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
  scope: ConfigurationScope;
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
  scope: ConfigurationScope;
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

export function getMCPServers(workspaceId: number | null, options?: RequestOptions): Promise<{ servers: MCPServer[] }> {
  return requestJson(configurationApiPath(workspaceId, "/mcp-servers"), options);
}

/**
 * 只取可编辑字段：调用方可以直接传带展示字段的 `MCPServer`，请求体在这一处收窄（后端 extra=forbid，
 * 不能带 `status` / `scope` 等展示字段）。
 */
function mcpServerConfiguration(server: MCPServerConfiguration): MCPServerConfiguration {
  return {
    name: server.name,
    transport: server.transport,
    command: server.command,
    args: server.args,
    env: server.env,
    url: server.url,
    headers: server.headers,
    oauth: server.oauth,
    enabled: server.enabled,
  };
}

/** 整表替换本作用域自己的服务；调用方只传本作用域的条目，不能混入继承来的系统级服务。 */
export function saveMCPServers(workspaceId: number | null, servers: MCPServerConfiguration[]): Promise<{ servers: MCPServer[] }> {
  return requestJson(
    configurationApiPath(workspaceId, "/mcp-servers"),
    jsonRequestInit({ servers: servers.map(mcpServerConfiguration) }, { method: "PUT" }),
  );
}

/** 连接测试：从不打开浏览器，需要授权时结果的 `failure_kind` 为 `authorization_required`。 */
export function testMCPServer(workspaceId: number | null, server: MCPServerConfiguration): Promise<MCPServerTestResult> {
  return requestJson(
    configurationApiPath(workspaceId, "/mcp-servers/test"),
    jsonRequestInit(mcpServerConfiguration(server), { method: "POST", timeoutMs: LONG_OPERATION_TIMEOUT_MS }),
  );
}

/** 开始登录授权：可能直接得出结果（已有可用令牌），也可能要用户去浏览器（按 `status` 区分）。 */
export function authorizeMCPServer(
  workspaceId: number | null,
  server: MCPServerConfiguration,
): Promise<MCPServerTestResult | MCPAuthorizationPending> {
  return requestJson(
    configurationApiPath(workspaceId, "/mcp-servers/authorize"),
    jsonRequestInit(mcpServerConfiguration(server), { method: "POST", timeoutMs: LONG_OPERATION_TIMEOUT_MS }),
  );
}

/** 浏览器授权最长等 5 分钟（后端时限），另留链路余量。 */
const AUTHORIZATION_WAIT_TIMEOUT_MS = 330_000;

/** 等一次登录授权结束；授权 id 全局唯一，不分作用域。 */
export function waitMCPAuthorization(flowId: string): Promise<MCPServerTestResult> {
  return requestJson(
    `/configuration/mcp-authorizations/${encodeURIComponent(flowId)}/wait`,
    jsonRequestInit({}, { method: "POST", timeoutMs: AUTHORIZATION_WAIT_TIMEOUT_MS }),
  );
}

/** 放弃一次登录授权：后端结束等待、关掉回调监听。 */
export function cancelMCPAuthorization(flowId: string): Promise<Record<string, never>> {
  return requestJson(`/configuration/mcp-authorizations/${encodeURIComponent(flowId)}`, { method: "DELETE" });
}

export function getSkills(workspaceId: number | null, options?: RequestOptions): Promise<Skill[]> {
  return requestJson(configurationApiPath(workspaceId, "/skills"), options);
}

export function getSkill(workspaceId: number | null, name: string, options?: RequestOptions): Promise<SkillDetail> {
  return requestJson(configurationApiPath(workspaceId, `/skills/${encodeURIComponent(name)}`), options);
}

/** Skill 目录内一个文本文件的只读预览；`path` 相对 Skill 目录。 */
export function getSkillFile(
  workspaceId: number | null,
  name: string,
  path: string,
  options?: RequestOptions,
): Promise<{ path: string; content: string }> {
  const query = new URLSearchParams({ path });
  return requestJson(configurationApiPath(workspaceId, `/skills/${encodeURIComponent(name)}/file?${query}`), options);
}

export function getSkillCatalog(workspaceId: number | null, options?: RequestOptions): Promise<SkillCatalogView> {
  return requestJson(configurationApiPath(workspaceId, "/skill-catalog"), options);
}

/** 一次启用或停用若干个本作用域的生效 Skill；含继承来的条目时后端整体拒绝，只写一次配置。 */
export function setSkillsEnabled(workspaceId: number | null, names: string[], enabled: boolean): Promise<Record<string, never>> {
  return requestJson(
    configurationApiPath(workspaceId, "/skills/enabled"),
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
  existing: { scope: ConfigurationScope; kind: SkillRootKind; location: string; description: string; same_content: boolean }[];
};

export function importSkill(
  workspaceId: number | null,
  source_path: string,
  source_type: "directory" | "archive",
  resolution?: SkillImportResolution,
): Promise<Skill> {
  return requestJson(
    configurationApiPath(workspaceId, "/skills/import"),
    jsonRequestInit({ source_path, source_type, resolution }, { method: "POST", timeoutMs: LONG_OPERATION_TIMEOUT_MS }),
  );
}

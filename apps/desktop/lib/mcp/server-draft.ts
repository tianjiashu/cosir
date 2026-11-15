import type { MCPServerConfiguration } from "@/lib/api/extensions";

/** 环境变量与请求头在表单里按行增删。 */
export type KeyValueRow = { name: string; value: string };

/**
 * 编辑框表单的草稿：参数按行编辑、环境变量与请求头按行增删、OAuth 四项拆成独立输入框，保存与测试时再
 * 经 `fromDraft` 转回 `MCPServerConfiguration`。
 */
export type ServerDraft = Omit<MCPServerConfiguration, "args" | "env" | "headers" | "oauth"> & {
  argsText: string;
  envRows: KeyValueRow[];
  headerRows: KeyValueRow[];
  oauthClientId: string;
  oauthClientSecret: string;
  oauthCallbackPort: string;
  oauthScope: string;
};

export const emptyDraft = (): ServerDraft => ({
  name: "",
  transport: "stdio",
  command: "",
  argsText: "",
  envRows: [],
  url: "",
  headerRows: [],
  oauthClientId: "",
  oauthClientSecret: "",
  oauthCallbackPort: "",
  oauthScope: "",
  enabled: true,
});

export function toDraft(server: MCPServerConfiguration): ServerDraft {
  return {
    name: server.name,
    transport: server.transport,
    command: server.command,
    argsText: server.args.join("\n"),
    envRows: Object.entries(server.env).map(([name, value]) => ({ name, value })),
    url: server.url,
    headerRows: Object.entries(server.headers).map(([name, value]) => ({ name, value })),
    oauthClientId: server.oauth?.client_id ?? "",
    oauthClientSecret: server.oauth?.client_secret ?? "",
    oauthCallbackPort: server.oauth?.callback_port?.toString() ?? "",
    oauthScope: server.oauth?.scope ?? "",
    enabled: server.enabled,
  };
}

/** 表单里 OAuth 四项都空即不配置（自动注册、随机回调端口）；stdio 服务没有 OAuth。 */
function oauthFromDraft(draft: ServerDraft): MCPServerConfiguration["oauth"] {
  const port = draft.oauthCallbackPort.trim();
  const oauth = { client_id: draft.oauthClientId.trim(), client_secret: draft.oauthClientSecret.trim(), callback_port: port ? Number(port) : null, scope: draft.oauthScope.trim() };
  if (draft.transport === "stdio" || (!oauth.client_id && !oauth.client_secret && oauth.callback_port === null && !oauth.scope)) return null;
  return oauth;
}

export function fromDraft(draft: ServerDraft): MCPServerConfiguration {
  return {
    name: draft.name.trim(),
    transport: draft.transport,
    command: draft.command.trim(),
    args: draft.argsText.split(/\r?\n/).filter((argument) => argument.length > 0),
    env: Object.fromEntries(draft.envRows.filter((row) => row.name.trim()).map((row) => [row.name.trim(), row.value])),
    url: draft.url.trim(),
    headers: Object.fromEntries(draft.headerRows.filter((row) => row.name.trim()).map((row) => [row.name.trim(), row.value])),
    oauth: oauthFromDraft(draft),
    enabled: draft.enabled,
  };
}

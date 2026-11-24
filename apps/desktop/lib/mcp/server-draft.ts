import type { MCPServerConfiguration } from "@/lib/api/extensions";

/** 环境变量与请求头在表单里按行增删。 */
export type KeyValueRow = { name: string; value: string };

/**
 * 编辑框表单的草稿：参数按行编辑、环境变量与请求头按行增删、OAuth 四项拆成独立输入框，保存与测试时再
 * 经 `fromDraft` 转回 `MCPServerConfiguration`。
 *
 * 参数的事实是 `args` 数组，`argsText` 只是文本框里正在敲的内容：两者只经 {@link withArgsText} 一起变，
 * 没在文本框里改过就原样保存数组。按行编辑表示不了空串与含换行的参数（JSON 方式能写），这时文本框只读
 * （{@link argsEditableAsLines}），只改名称等别处也不会把它们拆开或丢掉。
 */
export type ServerDraft = Omit<MCPServerConfiguration, "env" | "headers" | "oauth"> & {
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
  args: [],
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
    args: server.args,
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

/** 参数能否按行编辑：空串与含换行的参数写不进「每行一个」的文本框。 */
export function argsEditableAsLines(args: readonly string[]): boolean {
  return !args.some((argument) => argument === "" || /[\r\n]/.test(argument));
}

/** 文本框里改了参数：记下正在敲的文本，按行重新得到参数（空行不算参数）。 */
export function withArgsText(draft: ServerDraft, argsText: string): ServerDraft {
  return { ...draft, argsText, args: argsText.split(/\r?\n/).filter((argument) => argument.length > 0) };
}

/** 表单里 OAuth 四项都空即不配置（自动注册、随机回调端口）。 */
function oauthFromDraft(draft: ServerDraft): MCPServerConfiguration["oauth"] {
  const port = draft.oauthCallbackPort.trim();
  const oauth = { client_id: draft.oauthClientId.trim(), client_secret: draft.oauthClientSecret.trim(), callback_port: port ? Number(port) : null, scope: draft.oauthScope.trim() };
  if (!oauth.client_id && !oauth.client_secret && oauth.callback_port === null && !oauth.scope) return null;
  return oauth;
}

/**
 * 草稿转回配置，只带所选传输自己的字段（与 JSON 方式一致，也是后端的配置契约）：另一种传输填过的值留在草稿里，
 * 切回去还在，但不提交。
 */
export function fromDraft(draft: ServerDraft): MCPServerConfiguration {
  const rows = (items: KeyValueRow[]) => Object.fromEntries(items.filter((row) => row.name.trim()).map((row) => [row.name.trim(), row.value]));
  const base = { name: draft.name.trim(), transport: draft.transport, enabled: draft.enabled };
  if (draft.transport === "stdio") {
    return { ...base, command: draft.command.trim(), args: draft.args, env: rows(draft.envRows), url: "", headers: {}, oauth: null };
  }
  return { ...base, command: "", args: [], env: {}, url: draft.url.trim(), headers: rows(draft.headerRows), oauth: oauthFromDraft(draft) };
}

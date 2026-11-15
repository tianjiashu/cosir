/**
 * 配置的作用域：系统级，或某个工作区自己的一层。
 *
 * 配置页与配置接口都以 `workspaceId: number | null` 表达拥有者（null 为系统级，与查询键同一约定），
 * 作用域名与请求路径都由它推出，调用方不另传一份。工作区看到的 Skill / MCP 是两层合并的结果（同名时
 * 工作区覆盖系统级，`scope === "system"` 的条目在工作区页里是继承来的、只读）；子 Agent 列表只含本层。
 */
export type ConfigurationScope = "system" | "workspace";

/** 写操作只作用于请求所在的作用域。 */
export function scopeOf(workspaceId: number | null): ConfigurationScope {
  return workspaceId === null ? "system" : "workspace";
}

/** 配置接口路径：系统级在 `/configuration` 下，工作区级在 `/workspaces/{id}/configuration` 下。 */
export function configurationApiPath(workspaceId: number | null, path: string): string {
  return workspaceId === null ? `/configuration${path}` : `/workspaces/${workspaceId}/configuration${path}`;
}

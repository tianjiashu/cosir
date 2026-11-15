import type { QueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/lib/query/query-keys";

/**
 * Skill 与 MCP 写入或重新读取后，哪些查询要重拉：工作区页的列表含继承来的系统级条目，所以系统级的变化失效
 * 全部作用域（各工作区页与输入框选择器），工作区的变化只失效该工作区。规则只在这里，调用方不自己拼 key。
 */
export function invalidateSkills(queryClient: QueryClient, workspaceId: number | null): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: workspaceId === null ? queryKeys.skillsAll : queryKeys.skills(workspaceId) });
}

/** 同上；变量是系统级事实，改了变量传 `null`，全部作用域的变量来源与主状态都随之重读。 */
export function invalidateMcpServers(queryClient: QueryClient, workspaceId: number | null): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: workspaceId === null ? queryKeys.mcpServersAll : queryKeys.mcpServers(workspaceId) });
}

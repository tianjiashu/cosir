/**
 * 多处共用的查询：查询键与拉取函数在这里成对定义一次，调用方 `useQuery({ ...xxxQueryOptions(), enabled })`。
 * 这里定义的键只配这里的拉取函数，调用方不再各写一份，避免不同函数填充同一条缓存（尚未迁入的键见各调用点）。
 * 调用点各自的选项（`enabled`、`staleTime` 等）留在调用点。
 */
import { queryOptions } from "@tanstack/react-query";

import { getAgentConfigurations, getWorkspaceAgentConfigurations } from "@/lib/api/configuration";
import { getToolGroups } from "@/lib/api/tools";
import { getFileInfo } from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";

export const toolGroupsQueryOptions = () => queryOptions({
  queryKey: queryKeys.toolGroups(),
  queryFn: ({ signal }) => getToolGroups({ signal }),
});

/** 某一作用域自己的子 Agent 配置：`null` 为系统级，否则为该工作区的。 */
export const agentConfigurationsQueryOptions = (workspaceId: number | null) => queryOptions({
  queryKey: queryKeys.agents(workspaceId),
  queryFn: ({ signal }) => workspaceId === null
    ? getAgentConfigurations({ signal })
    : getWorkspaceAgentConfigurations(workspaceId, { signal }),
});

export const fileInfoQueryOptions = (workspaceId: number, path: string) => queryOptions({
  queryKey: queryKeys.fileInfo(workspaceId, path),
  queryFn: ({ signal }) => getFileInfo(workspaceId, path, signal),
});

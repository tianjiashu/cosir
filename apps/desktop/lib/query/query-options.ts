/**
 * 多处共用的查询：查询键与拉取函数在这里成对定义一次，调用方 `useQuery({ ...xxxQueryOptions(), enabled })`。
 * 这里定义的键只配这里的拉取函数，调用方不再各写一份：同一个键挂两个不同的拉取函数时，
 * 失效刷新会用最后挂上的那个观察者的函数，可能拉错数据或报错。
 * 调用点各自的选项（`enabled`、`staleTime` 等）留在调用点。
 */
import { queryOptions } from "@tanstack/react-query";

import { getAgentConfigurations } from "@/lib/api/configuration";
import { getToolGroups } from "@/lib/api/tool-groups";
import { getFileInfo } from "@/lib/api/workspace-files";
import { getMCPServers, getSkills } from "@/lib/api/extensions";
import { getModelConfigFile } from "@/lib/api/model-connections";
import { queryKeys } from "@/lib/query/query-keys";

export const toolGroupsQueryOptions = () => queryOptions({
  queryKey: queryKeys.toolGroups(),
  queryFn: ({ signal }) => getToolGroups({ signal }),
});

/** 某一作用域自己的子 Agent 配置：`null` 为系统级，否则为该工作区的。 */
export const agentConfigurationsQueryOptions = (workspaceId: number | null) => queryOptions({
  queryKey: queryKeys.agents(workspaceId),
  queryFn: ({ signal }) => getAgentConfigurations(workspaceId, { signal }),
});

export const fileInfoQueryOptions = (workspaceId: number, path: string) => queryOptions({
  queryKey: queryKeys.fileInfo(workspaceId, path),
  queryFn: ({ signal }) => getFileInfo(workspaceId, path, signal),
});

/** 某一作用域看得到的 Skill：`null` 为系统级，否则为该工作区的合并视图（含继承的系统级 Skill）。 */
export const skillsQueryOptions = (workspaceId: number | null) => queryOptions({
  queryKey: queryKeys.skills(workspaceId),
  queryFn: ({ signal }) => getSkills(workspaceId, { signal }),
});

/** 某一作用域看得到的 MCP 服务，含义同 {@link skillsQueryOptions}。 */
export const mcpServersQueryOptions = (workspaceId: number | null) => queryOptions({
  queryKey: queryKeys.mcpServers(workspaceId),
  queryFn: ({ signal }) => getMCPServers(workspaceId, { signal }),
});

/**
 * 模型配置文件可以在外部编辑器里手改：只对这一个查询开启窗口聚焦重拉，切回应用即看到改动
 * （各编辑卡的未保存内容是组件本地状态，不受重拉影响）。
 */
export const modelConnectionsQueryOptions = () => queryOptions({
  queryKey: queryKeys.modelConnections(),
  queryFn: ({ signal }) => getModelConfigFile({ signal }),
  refetchOnWindowFocus: true,
});

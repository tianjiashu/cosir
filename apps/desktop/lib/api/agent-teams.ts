import { configurationScope, type ConfigurationScope } from "@/lib/api/configuration";
import { jsonRequestInit, requestJson, type RequestOptions } from "@/lib/http/client";

export type AgentTeamStatusConfiguration = {
  name: string;
  color: string;
};

export type AgentTeamNodeConfiguration = {
  node_id: string;
  name: string;
  agent_id: string;
  statuses: AgentTeamStatusConfiguration[];
};

export type AgentTeamTransitionConfiguration = {
  from_node_id: string;
  status: string;
  target_node_id: string;
};

export type AgentTeamConfiguration = {
  team_id: string;
  name: string;
  description: string;
  max_runs: number;
  start_node_id: string;
  nodes: AgentTeamNodeConfiguration[];
  transitions: AgentTeamTransitionConfiguration[];
  scope: ConfigurationScope;
};

export type AgentTeamConfigurationDraft = Omit<AgentTeamConfiguration, "max_runs"> & {
  max_runs: number | string;
};

export type AgentTeamRun = {
  id: number;
  team_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  /** 终态原因；`user_rejected` 表示用户在审批卡片上驳回。 */
  end_reason?: string | null;
  goal: string;
  nodes: Array<{
    node_id: string;
    name: string;
    agent_id: string;
    role: string;
    goal_summary: string;
    status: string;
    visits: Array<{
      visit_number: number;
      conversation_run_id: number | null;
      status: string;
      agents: Array<{
        task_id: number | null;
        title: string;
        agent_id: string | null;
        role: string;
        status: string;
      }>;
    }>;
  }>;
  current_node_id: string | null;
};

/** 后端图结构问题；定位字段指向节点、状态出口或转移在草稿 transitions 中的下标。 */
export type AgentTeamGraphIssue = {
  code: string;
  message: string;
  node_id: string | null;
  status: string | null;
  transition_index: number | null;
};

type AgentTeamGraphDraft = Pick<AgentTeamConfigurationDraft, "start_node_id" | "nodes" | "transitions">;

/** 检查编辑中草稿的图结构，不保存、不区分作用域；规则与保存校验同源。 */
export function checkAgentTeamGraph(draft: AgentTeamGraphDraft, options?: RequestOptions): Promise<AgentTeamGraphIssue[]> {
  return requestJson<{ issues: AgentTeamGraphIssue[] }>(
    "/configuration/agent-teams/graph-issues",
    jsonRequestInit(draft, { method: "POST", ...options }),
  ).then((response) => response.issues);
}

function teamConfigurationPath(workspaceId: number | null, teamId?: string) {
  const root = workspaceId === null ? "/configuration/agent-teams" : `/workspaces/${encodeURIComponent(workspaceId)}/configuration/agent-teams`;
  return teamId === undefined ? root : `${root}/${encodeURIComponent(teamId)}`;
}

/** 写请求体：外层与配置里的 `scope` 都由拥有者推出，与路径一致。 */
function teamConfigurationInit(workspaceId: number | null, configuration: AgentTeamConfigurationDraft, method: "POST" | "PUT") {
  const scope = configurationScope(workspaceId);
  return jsonRequestInit({ scope, configuration: { ...configuration, scope } }, { method });
}

export function createAgentTeamConfiguration(
  workspaceId: number | null,
  configuration: AgentTeamConfigurationDraft,
): Promise<AgentTeamConfiguration> {
  return requestJson<AgentTeamConfiguration>(
    teamConfigurationPath(workspaceId),
    teamConfigurationInit(workspaceId, configuration, "POST"),
  );
}

export function updateAgentTeamConfiguration(
  workspaceId: number | null,
  teamId: string,
  configuration: AgentTeamConfigurationDraft,
): Promise<AgentTeamConfiguration> {
  return requestJson<AgentTeamConfiguration>(
    teamConfigurationPath(workspaceId, teamId),
    teamConfigurationInit(workspaceId, configuration, "PUT"),
  );
}

export function deleteAgentTeamConfiguration(workspaceId: number | null, teamId: string): Promise<void> {
  return requestJson(teamConfigurationPath(workspaceId, teamId), { method: "DELETE" }).then(() => undefined);
}

/** 工作区列表是该工作区可见的配置（含继承来的系统级），每项带自己的 `scope`。 */
export function getAgentTeamConfigurations(workspaceId: number | null, options?: RequestOptions): Promise<AgentTeamConfiguration[]> {
  return requestJson<AgentTeamConfiguration[]>(teamConfigurationPath(workspaceId), options);
}

export function getAgentTeamRun(runId: number, options?: RequestOptions): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(`/agent-team/runs/${encodeURIComponent(runId)}`, options);
}

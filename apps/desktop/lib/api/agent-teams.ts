import { jsonRequestInit, requestJson, type RequestOptions } from "@/lib/http/client";

export type AgentTeamConfigurationInput = {
  scope: "system" | "workspace";
  configuration: AgentTeamConfigurationDraft;
};

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
  scope: "system" | "workspace";
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

function teamConfigurationPath(scope: "system" | "workspace", workspaceId?: number, teamId?: string) {
  let root: string;
  if (scope === "system") {
    root = "/configuration/agent-teams";
  } else {
    if (workspaceId === undefined) throw new Error("workspace_id is required for workspace Team configuration");
    root = `/workspaces/${encodeURIComponent(workspaceId)}/configuration/agent-teams`;
  }
  return teamId === undefined ? root : `${root}/${encodeURIComponent(teamId)}`;
}

export function createAgentTeamConfiguration(
  input: AgentTeamConfigurationInput,
  workspaceId?: number,
): Promise<AgentTeamConfiguration> {
  return requestJson<AgentTeamConfiguration>(
    teamConfigurationPath(input.scope, workspaceId),
    jsonRequestInit(input, { method: "POST" }),
  );
}

export function updateAgentTeamConfiguration(
  teamId: string,
  input: AgentTeamConfigurationInput,
  workspaceId?: number,
): Promise<AgentTeamConfiguration> {
  return requestJson<AgentTeamConfiguration>(
    teamConfigurationPath(input.scope, workspaceId, teamId),
    jsonRequestInit(input, { method: "PUT" }),
  );
}

export function deleteAgentTeamConfiguration(
  teamId: string,
  scope: "system" | "workspace",
  workspaceId?: number,
): Promise<void> {
  return requestJson(teamConfigurationPath(scope, workspaceId, teamId), { method: "DELETE" }).then(() => undefined);
}

export function getSystemAgentTeamConfigurations(options?: RequestOptions): Promise<AgentTeamConfiguration[]> {
  return requestJson<AgentTeamConfiguration[]>("/configuration/agent-teams", options);
}

export function getWorkspaceAgentTeamConfigurations(
  workspaceId: number,
  options?: RequestOptions,
): Promise<AgentTeamConfiguration[]> {
  return requestJson<AgentTeamConfiguration[]>(
    `/workspaces/${encodeURIComponent(workspaceId)}/configuration/agent-teams`,
    options,
  );
}

export function getAgentTeamRun(runId: number, options?: RequestOptions): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(`/agent-team/runs/${encodeURIComponent(runId)}`, options);
}

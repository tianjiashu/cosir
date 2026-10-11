import { jsonRequestInit, requestJson } from "@/lib/http/client";

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
      business_status: string | null;
      business_status_color: string | null;
      failure_message: string | null;
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
  end_reason: string | null;
  failure_node_id: string | null;
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

export function getSystemAgentTeamConfigurations(): Promise<AgentTeamConfiguration[]> {
  return requestJson<AgentTeamConfiguration[]>("/configuration/agent-teams");
}

export function getWorkspaceAgentTeamConfigurations(workspaceId: number): Promise<AgentTeamConfiguration[]> {
  return requestJson<AgentTeamConfiguration[]>(
    `/workspaces/${encodeURIComponent(workspaceId)}/configuration/agent-teams`,
  );
}

export function getAgentTeamRun(runId: number): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(`/agent-team/runs/${encodeURIComponent(runId)}`);
}

export function cancelAgentTeamRun(runId: number): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(
    `/agent-team/runs/${encodeURIComponent(runId)}/cancel`,
    jsonRequestInit({}, { method: "POST" }),
  );
}

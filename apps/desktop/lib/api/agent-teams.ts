import { jsonRequestInit, requestJson } from "@/lib/http/client";

export type AgentTeamConfigurationInput = {
  scope: "system" | "workspace";
  configuration: AgentTeamConfigurationDraft;
};

export type AgentTeamNodeConfiguration = {
  node_id: string;
  name: string;
  agent_id: string;
  statuses: string[];
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
  workspace_id: number;
  parent_task_id: number;
  parent_run_id: number;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  active_node: {
    node_id: string;
    task_id: number;
    run_id: number;
    completed: false;
    status: null;
    output: null;
  } | null;
  node_results: Array<{
    node_id: string;
    task_id: number;
    run_id: number;
    completed: true;
    status: string;
    output: string;
  }>;
  state: Record<string, unknown>;
  end_reason: string | null;
  started_at: string | null;
  ended_at: string | null;
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

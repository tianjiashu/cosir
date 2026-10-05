import { jsonRequestInit, requestJson } from "@/lib/http/client";

export type AgentTeamConfigurationInput = {
  scope: "system" | "workspace";
  workspace_id?: number | null;
  configuration: Record<string, unknown>;
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

export function saveAgentTeamConfiguration(input: AgentTeamConfigurationInput): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>(
    "/configuration/agent-teams",
    jsonRequestInit(input, { method: "POST" }),
  );
}

export function confirmAgentTeamRun(
  parentTaskId: number,
  parentRunId: number,
  teamId: string,
  configuration: Record<string, unknown>,
): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(
    "/agent-team/runs/confirm",
    jsonRequestInit(
      {
        parent_task_id: parentTaskId,
        parent_run_id: parentRunId,
        team_id: teamId,
        configuration,
      },
      { method: "POST" },
    ),
  );
}

export function getLatestAgentTeamRun(
  parentTaskId: number,
  parentRunId: number,
  teamId: string,
): Promise<AgentTeamRun> {
  const query = new URLSearchParams({
    parent_task_id: String(parentTaskId),
    parent_run_id: String(parentRunId),
    team_id: teamId,
  });
  return requestJson<AgentTeamRun>(`/agent-team/runs/latest?${query.toString()}`);
}

export function getAgentTeamRun(runId: number): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(`/agent-team/runs/${encodeURIComponent(runId)}`);
}

export function waitAgentTeamRun(runId: number, timeoutSeconds?: number): Promise<AgentTeamRun> {
  const query = timeoutSeconds === undefined ? "" : `?timeout_seconds=${encodeURIComponent(timeoutSeconds)}`;
  return requestJson<AgentTeamRun>(
    `/agent-team/runs/${encodeURIComponent(runId)}/wait${query}`,
  );
}

export function cancelAgentTeamRun(runId: number): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(
    `/agent-team/runs/${encodeURIComponent(runId)}/cancel`,
    jsonRequestInit({}, { method: "POST" }),
  );
}

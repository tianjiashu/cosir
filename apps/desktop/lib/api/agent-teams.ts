import { jsonRequestInit, requestJson } from "@/lib/http/client";

export type AgentTeamConfigurationInput = {
  scope: "system" | "workspace";
  workspace_id?: number | null;
  configuration: Record<string, unknown>;
};

export type AgentTeamRun = {
  team_run_id: string;
  team_id: string;
  workspace_id: number;
  parent_task_id: number;
  parent_run_id: number;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  current_node_id: string | null;
  current_node_status: string | null;
  current_node_output: string | null;
  state: Record<string, unknown>;
  failure_kind: string | null;
  failure_message: string | null;
  started_at: string | null;
  ended_at: string | null;
};

export function saveAgentTeamConfiguration(input: AgentTeamConfigurationInput): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>(
    "/configuration/agent-teams",
    jsonRequestInit(input, { method: "POST" }),
  );
}

export function confirmAgentTeamPreview(
  parentTaskId: number,
  parentRunId: number,
  previewFingerprint: string,
): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(
    "/agent-team/previews/confirm",
    jsonRequestInit(
      {
        parent_task_id: parentTaskId,
        parent_run_id: parentRunId,
        preview_fingerprint: previewFingerprint,
      },
      { method: "POST" },
    ),
  );
}

export function getAgentTeamRun(teamRunId: string): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(`/agent-team/runs/${encodeURIComponent(teamRunId)}`);
}

export function getLatestAgentTeamRunByParent(
  parentTaskId: number,
  parentRunId: number,
  previewFingerprint: string,
): Promise<AgentTeamRun | null> {
  const query = new URLSearchParams({
    parent_task_id: String(parentTaskId),
    parent_run_id: String(parentRunId),
    preview_fingerprint: previewFingerprint,
  });
  return requestJson<AgentTeamRun | null>(`/agent-team/runs/by-parent?${query.toString()}`);
}

export function waitAgentTeamRun(teamRunId: string, timeoutSeconds?: number): Promise<AgentTeamRun> {
  const query = timeoutSeconds === undefined ? "" : `?timeout_seconds=${encodeURIComponent(timeoutSeconds)}`;
  return requestJson<AgentTeamRun>(
    `/agent-team/runs/${encodeURIComponent(teamRunId)}/wait${query}`,
  );
}

export function cancelAgentTeamRun(teamRunId: string): Promise<AgentTeamRun> {
  return requestJson<AgentTeamRun>(
    `/agent-team/runs/${encodeURIComponent(teamRunId)}/cancel`,
    jsonRequestInit({}, { method: "POST" }),
  );
}

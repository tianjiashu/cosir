import { asRecord } from "./types";

export type AgentTeamNodePreview = {
  nodeId: string;
  name: string;
  agentId: string;
  nodeType: "start" | "middle" | "end";
  role: string;
  effectiveModelName: string;
  effectiveTools: string[];
  statuses: string[];
  maxSteps: number;
};

export type AgentTeamPreviewDisplay = {
  kind: "agent-team-preview";
  teamRunId: number;
  teamId: string;
  name: string;
  goal: string;
  nodeGoals: Record<string, string>;
  startNode: string;
  parentTaskId: number;
  parentRunId: number;
  workspaceId: number;
  nodes: AgentTeamNodePreview[];
  edges: Record<string, unknown>[];
  configuration: Record<string, unknown>;
};

export type AgentTeamConfigurationDraftDisplay = {
  kind: "agent-team-configuration-draft";
  teamId: string;
  name: string;
  description: string;
  scope: "system" | "workspace";
  configuration: Record<string, unknown>;
  nodeCount: number;
};

function positiveId(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function positiveInteger(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
}

function stringList(value: unknown): string[] | undefined {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? value as string[]
    : undefined;
}

function recordList(value: unknown): Record<string, unknown>[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const records = value.filter((item): item is Record<string, unknown> => {
    return item !== null && typeof item === "object" && !Array.isArray(item);
  });
  return records.length === value.length ? records : undefined;
}

/** 严格读取 Team 预览；不信任工具结果中的任意正文作为执行参数。 */
export function readAgentTeamPreviewDisplay(value: unknown): AgentTeamPreviewDisplay | null {
  const data = asRecord(value);
  if (data.kind !== "agent-team-preview") return null;
  const teamRunId = positiveId(data.team_run_id);
  const teamId = text(data.team_id);
  const name = text(data.name);
  const goal = text(data.goal);
  const startNode = text(data.start_node);
  const parentTaskId = positiveId(data.parent_task_id);
  const parentRunId = positiveId(data.parent_run_id);
  const workspaceId = positiveId(data.workspace_id);
  const configuration = asRecord(data.configuration);
  if (teamRunId === undefined || !teamId || !name || !goal || !startNode || parentTaskId === undefined || parentRunId === undefined || workspaceId === undefined || Object.keys(configuration).length === 0) return null;
  const edges = recordList(data.edges);
  const nodeGoals = data.node_goals === undefined
    ? {}
    : Object.fromEntries(
      Object.entries(asRecord(data.node_goals)).filter((entry): entry is [string, string] => typeof entry[1] === "string"),
    );
  if (!Array.isArray(data.nodes) || !edges) return null;
  const nodes: AgentTeamNodePreview[] = [];
  for (const raw of data.nodes) {
    const node = asRecord(raw);
    const nodeId = text(node.node_id);
    const nodeName = text(node.name);
    const agentId = text(node.agent_id);
    const nodeType = node.node_type === "start" || node.node_type === "middle" || node.node_type === "end"
      ? node.node_type
      : undefined;
    const role = text(node.role);
    const effectiveModelName = text(node.effective_model_name);
    if (!nodeId || !nodeName || !agentId || !nodeType || !role || !effectiveModelName) return null;
    const effectiveTools = stringList(node.effective_tools);
    const statuses = stringList(node.statuses);
    const maxSteps = positiveInteger(node.max_steps);
    if (!effectiveTools || !statuses || maxSteps === undefined) return null;
    nodes.push({
      nodeId,
      name: nodeName,
      agentId,
      nodeType,
      role,
      effectiveModelName,
      effectiveTools,
      statuses,
      maxSteps,
    });
  }
  return {
    kind: "agent-team-preview",
    teamRunId,
    teamId,
    name,
    goal,
    nodeGoals,
    startNode,
    parentTaskId,
    parentRunId,
    workspaceId,
    nodes,
    edges,
    configuration,
  };
}

/** 严格读取 Team 配置候选展示数据。 */
export function readAgentTeamConfigurationDraftDisplay(
  value: unknown,
): AgentTeamConfigurationDraftDisplay | null {
  const data = asRecord(value);
  if (data.kind !== "agent-team-configuration-draft") return null;
  const teamId = text(data.team_id);
  const name = text(data.name);
  const description = text(data.description);
  const scope = data.scope === "system" || data.scope === "workspace" ? data.scope : undefined;
  const configuration = asRecord(data.configuration);
  if (!teamId || !name || !description || !scope || Object.keys(configuration).length === 0) return null;
  return {
    kind: "agent-team-configuration-draft",
    teamId,
    name,
    description,
    scope,
    configuration,
    nodeCount: Array.isArray(data.nodes) ? data.nodes.length : 0,
  };
}

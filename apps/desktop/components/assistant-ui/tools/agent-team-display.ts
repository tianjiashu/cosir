import { asRecord } from "./types";
import type { AgentTeamConfiguration, AgentTeamStatusConfiguration } from "@/lib/api/agent-teams";

export type AgentTeamNodePreview = {
  nodeId: string;
  name: string;
  role: string;
  statuses: AgentTeamStatusConfiguration[];
  maxSteps: number;
};

export type AgentTeamNodeGoal = {
  goal: string;
  summary: string;
};

export type AgentTeamPreviewDisplay = {
  kind: "agent-team-approval";
  teamRunId: number;
  teamId: string;
  name: string;
  goal: string;
  nodeGoals: Record<string, AgentTeamNodeGoal>;
  startNode: string;
  parentTaskId: number;
  parentRunId: number;
  workspaceId: number;
  nodes: AgentTeamNodePreview[];
  edges: Record<string, unknown>[];
  configuration: Record<string, unknown>;
};

/** 后端对一次「待用户决定」请求的声明（工具展示契约的 human-in-the-loop 部分）。 */
export type AgentTeamReviewRequest = {
  requestId: string;
  decisions: string[];
  draftSchema: string;
};

/** 已批准后的运行态展示（同一张卡从「待确认」翻到「运行中」，没有可编辑草稿）。 */
export type AgentTeamRunDisplay = {
  teamRunId: number;
  teamId: string;
  goal: string;
};

export type AgentTeamConfigurationDraftDisplay = {
  kind: "agent-team-configuration-draft";
  teamId: string;
  name: string;
  description: string;
  scope: "system" | "workspace";
  configuration: AgentTeamConfiguration;
  nodeCount: number;
};

function positiveId(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
}

/**
 * 载荷是否带着待决请求声明。
 *
 * `user_input_request` 的存在本身就是「这张卡等待用户作答」的标记：后端的展示投影只在该调用
 * 待作答时把它并进载荷，因此这里不再需要第二个布尔字段（两个表示会各自漂移）。
 */
function hasReviewRequest(data: Record<string, unknown>): boolean {
  const request = data.user_input_request;
  return typeof request === "object" && request !== null;
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

function statusList(value: unknown): AgentTeamStatusConfiguration[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const statuses = value.map((item) => {
    const status = asRecord(item);
    const name = text(status.name);
    const color = text(status.color);
    return name && color && /^#[0-9A-Fa-f]{6}$/.test(color)
      ? { name, color: color.toUpperCase() }
      : null;
  });
  return statuses.every((status) => status !== null)
    ? statuses as AgentTeamStatusConfiguration[]
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
  if (data.kind !== "agent-team-approval") return null;
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
  if (data.node_goals === null || typeof data.node_goals !== "object" || Array.isArray(data.node_goals)) return null;
  const nodeGoals: Record<string, AgentTeamNodeGoal> = {};
  for (const [nodeId, rawGoal] of Object.entries(asRecord(data.node_goals))) {
    const nodeGoal = asRecord(rawGoal);
    if (typeof nodeGoal.goal !== "string" || typeof nodeGoal.summary !== "string") return null;
    nodeGoals[nodeId] = { goal: nodeGoal.goal, summary: nodeGoal.summary };
  }
  if (!Array.isArray(data.nodes) || !edges) return null;
  const nodes: AgentTeamNodePreview[] = [];
  for (const raw of data.nodes) {
    const node = asRecord(raw);
    const nodeId = text(node.node_id);
    const nodeName = text(node.name);
    const role = text(node.role);
    if (!nodeId || !nodeName || !role) return null;
    const statuses = statusList(node.statuses);
    const maxSteps = positiveInteger(node.max_steps);
    if (!statuses || maxSteps === undefined) return null;
    nodes.push({
      nodeId,
      name: nodeName,
      role,
      statuses,
      maxSteps,
    });
  }
  if (nodes.length !== Object.keys(nodeGoals).length || nodes.some((node) => !nodeGoals[node.nodeId])) return null;
  return {
    kind: "agent-team-approval",
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

/**
 * 严格读取「待用户决定」请求标识。
 *
 * 前端提交决定时用 `request_id` 定位请求（不是从 `team_run_id` 反推）：请求标识是后端与前端
 * 之间的稳定契约，工具可以改变内部标识（例如未来不再用 TeamRun 主键）。
 */
export function readAgentTeamReviewRequest(value: unknown): AgentTeamReviewRequest | null {
  const data = asRecord(value);
  if (!hasReviewRequest(data)) return null;
  const request = asRecord(data.user_input_request);
  const requestId = text(request.request_id);
  const draftSchema = text(request.draft_schema);
  if (!requestId || !draftSchema) return null;
  const decisions = stringList(request.decisions);
  if (!decisions) return null;
  return { requestId, decisions, draftSchema };
}

/** 读取「用户已批准、Team 已启动」的运行态展示。 */
export function readAgentTeamRunDisplay(value: unknown): AgentTeamRunDisplay | null {
  const data = asRecord(value);
  if (data.kind !== "agent-team-execution") return null;
  if (hasReviewRequest(data)) return null;
  const teamRunId = positiveId(data.team_run_id);
  const teamId = text(data.team_id);
  if (teamRunId === undefined || !teamId) return null;
  return { teamRunId, teamId, goal: text(data.goal) ?? "" };
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
  const rawConfiguration = asRecord(data.configuration);
  if (!teamId || !name || !description || !scope) return null;
  const configurationScope = rawConfiguration.scope === "system" || rawConfiguration.scope === "workspace"
    ? rawConfiguration.scope
    : undefined;
  const configurationId = text(rawConfiguration.team_id);
  const configurationName = text(rawConfiguration.name);
  const configurationDescription = text(rawConfiguration.description);
  const startNodeId = text(rawConfiguration.start_node_id);
  const maxRuns = positiveInteger(rawConfiguration.max_runs);
  if (!configurationId || !configurationName || !configurationDescription || !startNodeId || !configurationScope || maxRuns === undefined) return null;
  if (configurationScope !== scope || configurationName !== name || configurationDescription !== description) return null;
  if (!Array.isArray(rawConfiguration.nodes) || !Array.isArray(rawConfiguration.transitions)) return null;
  const nodes = rawConfiguration.nodes.map((item) => {
    const node = asRecord(item);
    const nodeId = text(node.node_id);
    const nodeName = text(node.name);
    const agentId = text(node.agent_id);
    const statuses = statusList(node.statuses);
    return nodeId && nodeName && agentId && statuses
      ? { node_id: nodeId, name: nodeName, agent_id: agentId, statuses }
      : null;
  });
  if (nodes.length === 0 || nodes.some((node) => node === null)) return null;
  const transitions = rawConfiguration.transitions.map((item) => {
    const edge = asRecord(item);
    const fromNodeId = text(edge.from_node_id);
    const status = text(edge.status);
    const targetNodeId = text(edge.target_node_id);
    return fromNodeId && status && targetNodeId
      ? { from_node_id: fromNodeId, status, target_node_id: targetNodeId }
      : null;
  });
  if (transitions.some((edge) => edge === null)) return null;
  const configuration: AgentTeamConfiguration = {
    team_id: configurationId,
    name: configurationName,
    description: configurationDescription,
    max_runs: maxRuns,
    start_node_id: startNodeId,
    nodes: nodes as AgentTeamConfiguration["nodes"],
    transitions: transitions as AgentTeamConfiguration["transitions"],
    scope: configurationScope,
  };
  if (configuration.team_id !== teamId) return null;
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

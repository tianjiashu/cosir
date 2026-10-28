import type { ComponentType } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";

import { DelegationToolRow } from "./delegation-tool-row";
import { DetailsTool } from "./details-tool";
import { DiffTool } from "./diff-tool";
import { TerminalSessionTool } from "./terminal-session-tool";
import { TerminalTool } from "./terminal-tool";
import { ToolFallback } from "./tool-fallback";
import { AgentConfigurationDraftTool } from "./agent-configuration-draft-tool";
import { AgentTeamTool } from "./agent-team-tool";
import { AgentTeamExecutionCard } from "./agent-team-execution-card";
import { AgentTeamConfigurationTool } from "./agent-team-configuration-tool";
import { readAgentTeamConfigurationDraftDisplay, readAgentTeamPreviewDisplay } from "./agent-team-display";
import { readAgentConfigurationDraftDisplay } from "./agent-configuration-draft-display";
import { readChildAgentResultDisplay, readDelegationDisplay } from "./child-agent-display";
import { asRecord, readToolArtifact, type ToolArtifact } from "./types";

export type ToolPartRoute =
  | "diff"
  | "terminal"
  | "terminal-session"
  | "delegation"
  | "agent-configuration-draft"
  | "agent-team-approval"
  | "agent-team-execution"
  | "agent-team-configuration-draft"
  | "details"
  | "fallback";

export type ToolPartRendererProps = ToolCallMessagePartProps & {
  /** 拥有该工具 part 的任务：工具取消、审批决定与预览面板都按 Task 寻址。 */
  taskId: number;
  /** 只读展示面不提供写入交互（取消工具、停止子 Agent）。 */
  interactive?: boolean;
  /** 用户已请求停止整个 Task 的当前活动，工具级停止入口随之收起。 */
  taskCancelling?: boolean;
};

type ToolRendererMatchContext = {
  toolName: string;
  artifact: ToolArtifact;
  kind?: string;
};

export type ToolRendererDefinition = {
  route: ToolPartRoute;
  renderer: ComponentType<ToolPartRendererProps>;
  matches: (context: ToolRendererMatchContext) => boolean;
};

const KNOWN_DISPLAY_KINDS = new Set([
  "read-file-meta",
  "file-list",
  "content-search-results",
  "directory-list",
  "file-changes",
  "web-search-results",
  "web-extract-urls",
  "terminal-result",
  "terminal-session",
  "delegation-result",
  "child-agent-result",
  "agent-configuration-draft",
  "agent-team-approval",
  "agent-team-execution",
  "agent-team-configuration-draft",
  "mcp-tool-result",
  "skill-content",
]);

/**
 * 工具展示路由注册表。
 *
 * 条目只描述稳定 artifact 如何选择 renderer，不执行 IO、不读取 React
 * 状态，也不负责展示异常。新增工具展示时只需增加一个匹配条目和对应
 * renderer，ToolPart 本身不再继续增长工具分支。
 */
const FALLBACK_RENDERER: ToolRendererDefinition = {
  route: "fallback",
  renderer: ToolFallback,
  matches: () => true,
};

const TOOL_RENDERER_DEFINITIONS: readonly ToolRendererDefinition[] = [
  {
    route: "agent-team-configuration-draft",
    renderer: AgentTeamConfigurationTool,
    matches: ({ kind, artifact }) => kind === "agent-team-configuration-draft"
      && readAgentTeamConfigurationDraftDisplay(artifact.display_data) !== null,
  },
  {
    route: "agent-team-approval",
    renderer: AgentTeamTool,
    matches: ({ kind, artifact }) => kind === "agent-team-approval"
      && readAgentTeamPreviewDisplay(artifact.display_data) !== null,
  },
  {
    route: "agent-team-execution",
    renderer: AgentTeamExecutionCard,
    matches: ({ kind, artifact }) => kind === "agent-team-execution" || artifact.team_run_id !== undefined,
  },
  {
    route: "agent-configuration-draft",
    renderer: AgentConfigurationDraftTool,
    matches: ({ kind, artifact }) => kind === "agent-configuration-draft"
      && readAgentConfigurationDraftDisplay(artifact.display_data) !== null,
  },
  {
    route: "delegation",
    renderer: DelegationToolRow,
    matches: ({ kind, artifact }) => kind === "delegation-result"
      && readDelegationDisplay(artifact.display_data) !== null,
  },
  {
    route: "diff",
    renderer: DiffTool,
    matches: ({ kind, artifact }) => kind === "file-changes"
      || artifact.presentation.expand_layout === "diff",
  },
  {
    route: "terminal-session",
    renderer: TerminalSessionTool,
    matches: ({ kind }) => kind === "terminal-session",
  },
  {
    route: "terminal",
    renderer: TerminalTool,
    matches: ({ kind, artifact }) => kind === "terminal-result"
      || artifact.presentation.expand_layout === "terminal",
  },
  {
    route: "details",
    renderer: DetailsTool,
    matches: ({ kind, artifact }) => kind === "read-file-meta"
      || kind === "directory-list"
      || kind === "file-list"
      || kind === "content-search-results"
      || kind === "mcp-tool-result"
      || kind === "skill-content"
      || artifact.presentation.expand_layout === "details"
      || artifact.presentation.expand_layout === "list"
      || artifact.presentation.expand_layout === "write"
      || artifact.presentation.expand_layout === "none"
      || artifact.presentation.verb !== undefined
      || artifact.presentation.icon !== undefined,
  },
  FALLBACK_RENDERER,
];

/**
 * 将工具 artifact 路由到稳定的展示类别。
 *
 * 格式错误的 display_data、未知 kind 或专用 payload 校验失败时必须进入
 * fallback；不会猜测工具名称，也不会让不受信任的 presentation 绕过校验。
 */
export function routeToolPart(toolName: string, rawArtifact: unknown): ToolPartRoute {
  return resolveToolRenderer(toolName, rawArtifact).route;
}

/** 根据同一份注册定义解析实际 React renderer，避免路由和组件映射分离。 */
export function resolveToolRenderer(
  toolName: string,
  rawArtifact: unknown,
): ToolRendererDefinition {
  const artifact = readToolArtifact(rawArtifact);
  const displayData = asRecord(artifact.display_data);
  if (artifact.display_data !== null && Object.keys(displayData).length === 0) return FALLBACK_RENDERER;
  if (artifact.display_data !== null && "kind" in displayData && typeof displayData.kind !== "string") return FALLBACK_RENDERER;

  const kind = typeof displayData.kind === "string" ? displayData.kind : undefined;
  const fallback = FALLBACK_RENDERER;
  if (kind !== undefined && !KNOWN_DISPLAY_KINDS.has(kind)) return fallback;
  if (kind === "delegation-result" && readDelegationDisplay(artifact.display_data) === null) return fallback;
  if (kind === "child-agent-result" && readChildAgentResultDisplay(artifact.display_data) === null) return fallback;

  const context = { toolName, artifact, kind };
  return TOOL_RENDERER_DEFINITIONS.find((definition) => definition.matches(context)) ?? fallback;
}

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
import { DETAIL_VIEWS } from "./details/detail-views";

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

export type ToolRendererDefinition = {
  route: ToolPartRoute;
  renderer: ComponentType<ToolPartRendererProps>;
  /** 该 renderer 承接的 `display_data.kind`；所有条目的并集就是已知 kind 清单。 */
  kinds: readonly string[];
  /** kind 命中后的专用载荷校验，不通过时继续匹配后续条目，最终回落 fallback。 */
  accepts?: (artifact: ToolArtifact) => boolean;
  /** 不看 kind、按 part 身份命中（如 TeamRun 失败后 display_data 只剩短提示）。 */
  matchesPart?: (artifact: ToolArtifact) => boolean;
  /** display_data 尚无 kind（运行中、只有静态声明）时按 presentation 命中。 */
  matchesPresentation?: (artifact: ToolArtifact) => boolean;
};

/**
 * 工具展示路由注册表。
 *
 * 条目只描述稳定 artifact 如何选择 renderer，不执行 IO、不读取 React
 * 状态，也不负责展示异常。新增工具展示时只需增加一个条目和对应
 * renderer，ToolPart 本身不再继续增长工具分支。条目有序：审批卡排在执行卡之前，
 * 有效的审批载荷优先于 `team_run_id`。
 */
const FALLBACK_RENDERER: ToolRendererDefinition = {
  route: "fallback",
  renderer: ToolFallback,
  kinds: [],
};

const TOOL_RENDERER_DEFINITIONS: readonly ToolRendererDefinition[] = [
  {
    route: "agent-team-configuration-draft",
    renderer: AgentTeamConfigurationTool,
    kinds: ["agent-team-configuration-draft"],
    accepts: (artifact) => readAgentTeamConfigurationDraftDisplay(artifact.display_data) !== null,
  },
  {
    route: "agent-team-approval",
    renderer: AgentTeamTool,
    kinds: ["agent-team-approval"],
    accepts: (artifact) => readAgentTeamPreviewDisplay(artifact.display_data) !== null,
  },
  {
    route: "agent-team-execution",
    renderer: AgentTeamExecutionCard,
    kinds: ["agent-team-execution"],
    matchesPart: (artifact) => artifact.team_run_id !== undefined,
  },
  {
    route: "agent-configuration-draft",
    renderer: AgentConfigurationDraftTool,
    kinds: ["agent-configuration-draft"],
    accepts: (artifact) => readAgentConfigurationDraftDisplay(artifact.display_data) !== null,
  },
  {
    route: "delegation",
    renderer: DelegationToolRow,
    kinds: ["delegation-result"],
    accepts: (artifact) => readDelegationDisplay(artifact.display_data) !== null,
  },
  {
    route: "diff",
    renderer: DiffTool,
    kinds: ["file-changes"],
    matchesPresentation: (artifact) => artifact.presentation.expand_layout === "diff",
  },
  {
    route: "terminal-session",
    renderer: TerminalSessionTool,
    kinds: ["terminal-session"],
  },
  {
    route: "terminal",
    renderer: TerminalTool,
    kinds: ["terminal-result"],
    matchesPresentation: (artifact) => artifact.presentation.expand_layout === "terminal",
  },
  {
    route: "details",
    renderer: DetailsTool,
    kinds: Object.keys(DETAIL_VIEWS),
    accepts: (artifact) => asRecord(artifact.display_data).kind !== "child-agent-result"
      || readChildAgentResultDisplay(artifact.display_data) !== null,
    matchesPresentation: ({ presentation }) => presentation.expand_layout === "details"
      || presentation.expand_layout === "list"
      || presentation.expand_layout === "write"
      || presentation.expand_layout === "none"
      || presentation.verb !== undefined
      || presentation.icon !== undefined,
  },
];

const KNOWN_DISPLAY_KINDS = new Set(TOOL_RENDERER_DEFINITIONS.flatMap((definition) => definition.kinds));

function matchesDefinition(definition: ToolRendererDefinition, artifact: ToolArtifact, kind: string | undefined): boolean {
  if (kind !== undefined && definition.kinds.includes(kind)) return definition.accepts?.(artifact) ?? true;
  if (definition.matchesPart?.(artifact)) return true;
  return kind === undefined && (definition.matchesPresentation?.(artifact) ?? false);
}

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
  if (kind !== undefined && !KNOWN_DISPLAY_KINDS.has(kind)) return FALLBACK_RENDERER;
  return TOOL_RENDERER_DEFINITIONS.find((definition) => matchesDefinition(definition, artifact, kind)) ?? FALLBACK_RENDERER;
}

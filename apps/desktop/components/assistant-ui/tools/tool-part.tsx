import { memo } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { DetailsTool } from "./details-tool";
import { DiffTool } from "./diff-tool";
import { TerminalTool } from "./terminal-tool";
import { TerminalSessionTool } from "./terminal-session-tool";
import { ToolFallback } from "./tool-fallback";
import { DelegationToolRow } from "./delegation-tool-row";
import { readToolArtifact } from "./types";

export type ToolPartRoute = "diff" | "terminal" | "terminal-session" | "delegation" | "details" | "fallback";
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
  "repeated-call",
]);

/**
 * 依据后端稳定工具名、data.kind 和 presentation 语义选择只读 renderer。
 * `expand_layout=none` 只决定紧凑展示，不决定工具语义。
 */
export function routeToolPart(toolName: string, rawArtifact: unknown): ToolPartRoute {
  // Keep the stable call shape for callers; routing intentionally ignores tool names.
  void toolName;
  const artifact = readToolArtifact(rawArtifact);
  const kind = typeof artifact.display_data?.kind === "string" ? artifact.display_data.kind : undefined;
  if (kind !== undefined && !KNOWN_DISPLAY_KINDS.has(kind)) return "fallback";
  if (kind === "delegation-result") return "delegation";
  if (kind === "file-changes" || artifact.presentation.expand_layout === "diff") return "diff";
  if (kind === "terminal-session") return "terminal-session";
  if (kind === "terminal-result" || artifact.presentation.expand_layout === "terminal") return "terminal";
  if (
    kind === "read-file-meta" ||
    kind === "directory-list" ||
    kind === "file-list" ||
    kind === "content-search-results" ||
    artifact.presentation.expand_layout === "details" ||
    artifact.presentation.expand_layout === "list" ||
    artifact.presentation.expand_layout === "write" ||
    artifact.presentation.expand_layout === "none" ||
    artifact.presentation.verb !== undefined ||
    artifact.presentation.icon !== undefined
  ) return "details";
  return "fallback";
}

type ToolPartProps = ToolCallMessagePartProps & {
  /** Run owning the message; absent in read-only surfaces that cannot cancel tools. */
  runId?: number | null;
  /** Whole-run cancellation is already in progress. */
  runCancelling?: boolean;
  /** Task owning the tool part, used only to open its task-scoped preview panel. */
  taskId?: number;
};

const ToolPartImpl = (props: ToolPartProps) => {
  switch (routeToolPart(props.toolName, props.artifact)) {
    case "diff":
      return <DiffTool {...props} />;
    case "terminal":
      return <TerminalTool {...props} />;
    case "terminal-session":
      return <TerminalSessionTool {...props} />;
    case "delegation":
      return <DelegationToolRow {...props} />;
    case "details":
      return <DetailsTool {...props} />;
    case "fallback":
      return <ToolFallback {...props} />;
  }
};

export const ToolPart = memo(ToolPartImpl);

import { memo } from "react";
import type { ToolCallMessagePartComponent } from "@assistant-ui/react";
import { DetailsTool } from "./details-tool";
import { DiffTool } from "./diff-tool";
import { DeleteTool } from "./delete-tool";
import { TerminalTool } from "./terminal-tool";
import { ToolFallback } from "./tool-fallback";
import { readToolArtifact } from "./types";

export type ToolPartRoute = "delete" | "diff" | "terminal" | "details" | "fallback";

const DELETE_TOOL_NAMES = new Set(["delete", "delete_file"]);
const KNOWN_DISPLAY_KINDS = new Set([
  "read-file-meta",
  "file-list",
  "directory-list",
  "file-changes",
  "delete-result",
  "web-search-results",
  "web-extract-urls",
  "terminal-result",
  "delegation-result",
  "repeated-call",
]);

/**
 * 依据后端稳定工具名、data.kind 和 presentation 语义选择只读 renderer。
 * `expand_layout=none` 仅表示不可展开，绝不意味着删除。
 */
export function routeToolPart(toolName: string, rawArtifact: unknown): ToolPartRoute {
  const artifact = readToolArtifact(rawArtifact);
  const kind = typeof artifact.display_data?.kind === "string" ? artifact.display_data.kind : undefined;
  if (kind !== undefined && !KNOWN_DISPLAY_KINDS.has(kind)) return "fallback";
  if (DELETE_TOOL_NAMES.has(toolName) || kind === "delete-result") return "delete";
  if (kind === "file-changes" || artifact.presentation.expand_layout === "diff") return "diff";
  if (kind === "terminal-result" || artifact.presentation.expand_layout === "terminal") return "terminal";
  if (
    kind === "read-file-meta" ||
    kind === "directory-list" ||
    kind === "file-list" ||
    artifact.presentation.expand_layout === "details" ||
    artifact.presentation.expand_layout === "list" ||
    artifact.presentation.expand_layout === "write" ||
    artifact.presentation.expand_layout === "none" ||
    artifact.presentation.verb !== undefined ||
    artifact.presentation.icon !== undefined
  ) return "details";
  return "fallback";
}

const ToolPartImpl: ToolCallMessagePartComponent = (props) => {
  switch (routeToolPart(props.toolName, props.artifact)) {
    case "delete":
      return <DeleteTool {...props} />;
    case "diff":
      return <DiffTool {...props} />;
    case "terminal":
      return <TerminalTool {...props} />;
    case "details":
      return <DetailsTool {...props} />;
    case "fallback":
      return <ToolFallback {...props} />;
  }
};

export const ToolPart = memo(ToolPartImpl);

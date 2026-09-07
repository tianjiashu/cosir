import { memo } from "react";
import type { ToolCallMessagePartComponent } from "@assistant-ui/react";
import { DetailsTool } from "./details-tool";
import { DiffTool } from "./diff-tool";
import { DeleteTool } from "./delete-tool";
import { TerminalTool } from "./terminal-tool";
import { UnknownTool } from "./unknown-tool";
import { readToolArtifact } from "./types";

export type ToolPartRoute = "delete" | "diff" | "terminal" | "details" | "unknown";

const DELETE_TOOL_NAMES = new Set(["delete", "delete_file"]);

/**
 * 依据后端稳定工具名、data.kind 和 presentation 语义选择只读 renderer。
 * `expand_layout=none` 仅表示不可展开，绝不意味着删除。
 */
export function routeToolPart(toolName: string, rawArtifact: unknown): ToolPartRoute {
  const artifact = readToolArtifact(rawArtifact);
  const kind = typeof artifact.data?.kind === "string" ? artifact.data.kind : undefined;
  if (DELETE_TOOL_NAMES.has(toolName) || kind === "delete-result") return "delete";
  if (kind === "file-changes" || artifact.presentation.expand_layout === "diff") return "diff";
  if (kind === "terminal-result" || artifact.presentation.expand_layout === "terminal") return "terminal";
  if (
    kind === "read-file-meta" ||
    kind === "directory-list" ||
    kind === "file-list" ||
    artifact.presentation.expand_layout === "details" ||
    artifact.presentation.expand_layout === "list" ||
    artifact.presentation.expand_layout === "write"
  ) return "details";
  return "unknown";
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
    case "unknown":
      return <UnknownTool {...props} />;
  }
};

export const ToolPart = memo(ToolPartImpl);

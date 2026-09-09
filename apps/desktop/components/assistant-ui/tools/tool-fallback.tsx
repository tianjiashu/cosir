import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { readToolArtifact } from "./types";
import { ToolIcon } from "./tool-icons";
import { ToolStatus } from "./tool-status";

/** Render-only fallback for a declared tool without a dedicated rich renderer. */
export function ToolFallback({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const title = artifact.presentation.verb ?? toolName;
  const label = artifact.backendStatus === "failed" ? `工具执行失败：${title}` : title;
  const meta = artifact.error
    ?? (Object.keys(artifact.presentation).length > 0
      ? "仅展示后端状态，不执行客户端工具"
      : `暂无专用展示器：${toolName}`);

  return (
    <DisclosureRowStatic
      role="status"
      leading={<ToolIcon name={artifact.presentation.icon} aria-hidden="true" />}
      label={<span className="text-foreground text-sm font-medium">{label}</span>}
      meta={<span className="text-muted-foreground max-w-[45%] truncate">{meta}</span>}
      trailing={<ToolStatus status={artifact.backendStatus} />}
      className={artifact.backendStatus === "failed" ? "text-destructive" : undefined}
    />
  );
}

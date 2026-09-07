import { Trash2Icon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";

export function DeleteTool({ toolName, args, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.data ?? {};
  const target = typeof data.path === "string" ? data.path : typeof args?.path === "string" ? args.path : "目标路径未知";
  const recursive = data.recursive === true || asRecord(args).recursive === true;
  return (
    <div className="flex items-center gap-2 py-1.5 text-muted-foreground">
      <Trash2Icon className="text-destructive size-4 shrink-0" aria-hidden="true" />
      <span className="text-foreground truncate text-sm font-medium">{artifact.presentation.verb ?? toolName}</span>
      <span className="min-w-0 truncate text-xs">{target}</span>
      {recursive && <span className="text-muted-foreground shrink-0 text-xs">递归删除目录</span>}
      <span className="ml-auto shrink-0"><ToolStatus status={artifact.backendStatus} /></span>
    </div>
  );
}

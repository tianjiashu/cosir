import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { CircleAlertIcon } from "lucide-react";
import { readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";

/** 未知工具的只读兜底；不执行工具，也不猜测其用途或破坏性。 */
export function UnknownTool({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  return (
    <div role="status" className="flex min-w-0 items-center gap-2 py-1.5">
      <CircleAlertIcon className="size-4 shrink-0 text-destructive" aria-hidden="true" />
      <span className="text-foreground truncate text-sm font-medium">未知工具：{toolName}</span>
      <span className="text-muted-foreground min-w-0 truncate text-xs">仅展示后端状态，不执行客户端工具</span>
      <span className="ml-auto shrink-0"><ToolStatus status={artifact.backendStatus} /></span>
    </div>
  );
}

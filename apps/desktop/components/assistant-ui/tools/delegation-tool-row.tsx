import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { UsersIcon } from "lucide-react";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { frontendLog } from "@/lib/logging/frontend-log";
import { ToolActivityRow } from "./tool-activity-row";
import { asRecord, readToolArtifact } from "./types";

/** Generic low-noise tool activity row adapter for delegation. */
export function DelegationToolRow({ artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = asRecord(artifact.display_data);
  const actions = useWorkbenchActions();
  const title = typeof data.title === "string" && data.title.trim() ? data.title : "子 Agent";
  const role = artifact.agent_role ?? (typeof data.role === "string" ? data.role : null);
  const childTaskId = artifact.child_task_id ?? (typeof data.child_task_id === "number" ? data.child_task_id : undefined);
  const canOpen = childTaskId !== undefined && actions !== null && actions.workspaceId !== null;
  const open = () => {
    if (canOpen && childTaskId !== undefined) {
      void frontendLog("INFO", "workbench_agent_open_requested", "主 Thread 请求打开子 Agent Workbench", {
        data: { childTaskId },
      });
      actions?.openAgent({ taskId: childTaskId, title, role });
    }
  };
  return <ToolActivityRow
    icon={<UsersIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />}
    title={title}
    meta={role ?? "角色未知"}
    status={artifact.backendStatus}
    onOpen={canOpen ? open : null}
    openLabel={`打开子 Agent：${title}`}
    disabled={!canOpen}
    className={artifact.backendStatus === "failed" ? "text-destructive" : undefined}
  />;
}

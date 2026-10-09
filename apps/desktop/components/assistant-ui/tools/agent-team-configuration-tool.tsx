import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { PencilIcon, UsersIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { useWorkbenchStore } from "@/lib/workbench/store";
import { readToolArtifact } from "./types";
import { readAgentTeamConfigurationDraftDisplay } from "./agent-team-display";

/** Team 配置候选卡；编辑和保存由 Workbench 负责。 */
export function AgentTeamConfigurationTool({ artifact: rawArtifact, taskId, toolCallId }: ToolCallMessagePartProps & { taskId?: number }) {
  const artifact = readToolArtifact(rawArtifact);
  const draft = readAgentTeamConfigurationDraftDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const saved = useWorkbenchStore((state) => state.savedDraftToolCallIds.includes(toolCallId));
  if (!draft) return null;
  const canEdit = taskId !== undefined && actions !== null && actions.workspaceId !== null;
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border/70 bg-card px-3 py-2.5 shadow-sm">
      <UsersIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{draft.name}</div>
        <p className="text-muted-foreground truncate text-xs">{draft.description} · {draft.nodeCount} 个节点 · {saved ? "已保存" : "未保存草稿"}</p>
      </div>
      <Button type="button" variant="outline" size="sm" disabled={!canEdit} onClick={() => {
        if (!canEdit || taskId === undefined) return;
        actions.openAgentTeamConfigurationDraft({
          taskId,
          toolCallId,
          draft: draft.configuration,
        });
      }}>
        <PencilIcon className="size-3.5" />编辑配置
      </Button>
    </div>
  );
}

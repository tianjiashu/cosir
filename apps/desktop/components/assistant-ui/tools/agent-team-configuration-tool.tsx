import { PencilIcon, UsersIcon } from "lucide-react";

import { SURFACE_CARD_CLASS } from "@/components/assistant-ui/turn/turn-tokens";
import { Button } from "@/components/ui/button";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { useDraftSaved } from "@/lib/workbench/drafts";
import { cn } from "@/lib/utils";
import { readAgentTeamConfigurationDraftDisplay } from "./agent-team-display";
import type { ArtifactCardProps } from "./artifact-card";

/** 产物区的 Team 配置候选卡；编辑和保存由 Workbench 负责。 */
export function AgentTeamConfigurationTool({ artifact, taskId, toolCallId }: ArtifactCardProps) {
  const draft = readAgentTeamConfigurationDraftDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const saved = useDraftSaved(actions?.workspaceId ?? null, toolCallId);
  if (!draft) return null;
  const canEdit = actions !== null;
  return (
    <div data-slot="agent-team-configuration-draft-card" className={cn(SURFACE_CARD_CLASS, "flex items-center gap-3 px-3 py-2.5")}>
      <UsersIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{draft.name}</div>
        <p className="text-muted-foreground truncate text-xs">{draft.description} · {draft.nodeCount} 个节点 · {saved ? "已保存" : "未保存草稿"}</p>
      </div>
      <Button type="button" variant="outline" size="sm" disabled={!canEdit} onClick={() => {
        if (!canEdit) return;
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

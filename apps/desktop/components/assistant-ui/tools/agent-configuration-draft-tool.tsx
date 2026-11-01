import { BotIcon, PencilIcon } from "lucide-react";

import { SURFACE_CARD_CLASS } from "@/components/assistant-ui/turn/turn-tokens";
import { Button } from "@/components/ui/button";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { useDraftSaved } from "@/lib/workbench/drafts";
import { cn } from "@/lib/utils";
import { readAgentConfigurationDraftDisplay } from "./agent-configuration-draft-display";
import type { ArtifactCardProps } from "./artifact-card";

/** 产物区的只读配置提案卡；编辑和保存都委托给 Workbench。 */
export function AgentConfigurationDraftTool({ artifact, taskId, toolCallId }: ArtifactCardProps) {
  const draft = readAgentConfigurationDraftDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const saved = useDraftSaved(actions?.workspaceId ?? null, toolCallId);
  if (!draft) return null;
  const canEdit = actions !== null;
  return (
    <div data-slot="agent-configuration-draft-card" className={cn(SURFACE_CARD_CLASS, "flex items-center gap-3 px-3 py-2.5")}>
      <BotIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{draft.agent_id}</div>
        <p className="text-muted-foreground truncate text-xs">{draft.role} · {draft.description} · {saved ? "已保存" : "未保存草稿"}</p>
      </div>
      {canEdit && <div className="flex shrink-0 gap-1.5"><Button type="button" variant="outline" size="sm" className="h-7 gap-1.5 px-2" onClick={() => actions.openAgentConfigurationDraft({ taskId, toolCallId, draft: { agent_id: draft.agent_id, role: draft.role, description: draft.description, system_prompt: draft.system_prompt } })}><PencilIcon className="size-3.5" />编辑配置</Button></div>}
    </div>
  );
}

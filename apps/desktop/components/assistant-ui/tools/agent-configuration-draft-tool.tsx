import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { BotIcon, PencilIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { useWorkbenchStore } from "@/lib/workbench/store";
import { readToolArtifact } from "./types";
import { readAgentConfigurationDraftDisplay } from "./agent-configuration-draft-display";
import { ToolStatus } from "./tool-status";

type AgentConfigurationDraftToolProps = ToolCallMessagePartProps & { taskId?: number };

/** 对话中的只读配置提案卡；编辑和保存都委托给 Workbench。 */
export function AgentConfigurationDraftTool({ artifact: rawArtifact, taskId, toolCallId }: AgentConfigurationDraftToolProps) {
  const artifact = readToolArtifact(rawArtifact);
  const draft = readAgentConfigurationDraftDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const draftTab = useWorkbenchStore((state) => {
    const tab = state.tabs.find((candidate) => candidate.kind === "agent-configuration-draft" && candidate.toolCallId === toolCallId);
    return tab?.kind === "agent-configuration-draft" ? tab : undefined;
  });
  const saved = useWorkbenchStore((state) => state.savedDraftToolCallIds.includes(toolCallId));
  const dismissed = useWorkbenchStore((state) => state.dismissedDraftToolCallIds.includes(toolCallId));
  const dismiss = useWorkbenchStore((state) => state.dismissAgentConfigurationDraft);
  if (!draft) return null;
  if (dismissed) return null;
  const canEdit = taskId !== undefined && actions !== null && actions.workspaceId !== null;
  const discard = () => {
    if (draftTab?.dirty && !window.confirm("配置草稿尚未保存，确定放弃吗？")) return;
    dismiss(toolCallId);
  };
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border/70 bg-card px-3 py-2.5 shadow-sm">
      <BotIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2"><span className="truncate text-sm font-medium">{draft.agent_id}</span><ToolStatus status={artifact.backendStatus} className="text-muted-foreground" /></div>
        <p className="text-muted-foreground truncate text-xs">{draft.role} · {draft.description} · {saved ? "已保存" : "未保存草稿"}</p>
      </div>
      {canEdit && taskId !== undefined && <div className="flex shrink-0 gap-1.5"><Button type="button" variant="outline" size="sm" className="h-7 gap-1.5 px-2" onClick={() => actions?.openAgentConfigurationDraft({ taskId, toolCallId, draft: { agent_id: draft.agent_id, role: draft.role, description: draft.description, system_prompt: draft.system_prompt } })}><PencilIcon className="size-3.5" />编辑配置</Button><Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-muted-foreground" onClick={discard}>放弃</Button></div>}
    </div>
  );
}

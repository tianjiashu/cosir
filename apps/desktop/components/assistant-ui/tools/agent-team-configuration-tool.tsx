import { useState } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { SaveIcon, UsersIcon } from "lucide-react";

import { saveAgentTeamConfiguration } from "@/lib/api/agent-teams";
import { Button } from "@/components/ui/button";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { readToolArtifact } from "./types";
import { readAgentTeamConfigurationDraftDisplay } from "./agent-team-display";

/** Team 配置候选卡；只有用户点击保存后才写入 system/workspace JSON。 */
export function AgentTeamConfigurationTool({ artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const draft = readAgentTeamConfigurationDraftDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const [message, setMessage] = useState("");
  if (!draft) return null;
  const canSave = draft.scope === "system"
    || (actions?.workspaceId !== null && actions?.workspaceId !== undefined);
  const save = async () => {
    if (!canSave) return;
    setState("saving");
    setMessage("");
    try {
      await saveAgentTeamConfiguration({
        scope: draft.scope,
        workspace_id: draft.scope === "workspace" ? actions?.workspaceId : null,
        configuration: draft.configuration,
      });
      setState("saved");
      setMessage("已保存，可在后续 agent_team 调用中复用");
    } catch (error) {
      setState("failed");
      setMessage(error instanceof Error ? error.message : "保存失败");
    }
  };
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border/70 bg-card px-3 py-2.5 shadow-sm">
      <UsersIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{draft.name}</div>
        <p className="text-muted-foreground truncate text-xs">{draft.description} · {draft.nodeCount} 个节点 · {draft.scope}</p>
        {message && <p className={state === "failed" ? "text-destructive text-xs" : "text-emerald-600 text-xs"}>{message}</p>}
      </div>
      <Button type="button" variant="outline" size="sm" disabled={!canSave || state === "saving" || state === "saved"} onClick={save}>
        <SaveIcon className="size-3.5" />{state === "saved" ? "已保存" : state === "saving" ? "保存中…" : "保存配置"}
      </Button>
    </div>
  );
}

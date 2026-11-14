import { useDraftSaved } from "@/lib/workbench/drafts";

/** 仅拥有该草稿的记录订阅保存状态；继承记录不挂载此组件。 */
export function DraftSaveStatus({ workspaceId, sessionId, toolCallId }: { workspaceId: number | null; sessionId: number; toolCallId: string }) {
  const saved = useDraftSaved(workspaceId, sessionId, toolCallId);
  return <>{saved ? "已保存" : "未保存草稿"}</>;
}

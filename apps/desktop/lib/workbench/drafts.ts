import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useWorkbenchStore } from "./store";
import type { WorkbenchTab } from "./types";

/** 草稿是否未保存由会话持有，判定不依赖标签是否激活或面板是否挂载。 */
export function isWorkbenchDraftDirty(tab: WorkbenchTab): boolean {
  if (tab.kind === "agent-configuration-draft") return tab.dirty;
  if (tab.kind === "agent-team-configuration-draft") return tab.session.store.getState().dirty;
  return false;
}

export function hasWorkbenchDrafts(): boolean {
  return useWorkbenchStore.getState().tabs.some(isWorkbenchDraftDirty);
}

/** 仅确认真正切换工作区的丢弃意图；会话清理由导航提交后的 ensureWorkspace 负责。 */
export async function confirmWorkbenchWorkspaceChange(nextWorkspaceId: number | null): Promise<boolean> {
  const { workspaceId, tabs } = useWorkbenchStore.getState();
  if (workspaceId === nextWorkspaceId) return true;
  const dirty = tabs.filter(isWorkbenchDraftDirty);
  if (dirty.length === 0) return true;
  const confirmed = await requestConfirmation({
    title: "切换工作区并放弃草稿？",
    description: `以下配置草稿尚未保存：${dirty.map(tab => `「${tab.title}」`).join("、")}。切换后这些修改会丢失。`,
    confirmLabel: "放弃草稿并切换",
    tone: "destructive",
    icon: "discard",
  });
  void frontendLog("INFO", "workbench_workspace_change_confirmed", "工作台工作区切换确认结束", {
    data: { workspaceId, nextWorkspaceId, confirmed, draftCount: dirty.length },
  });
  return confirmed;
}

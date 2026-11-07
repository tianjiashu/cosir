import type { ModelCatalogSnapshot } from "@/lib/model/catalog";

/** 模型目录已返回但没有可用模型；加载中不算，避免目录尚未返回就催用户去配置。 */
export function modelUnavailable({ status, catalog }: Pick<ModelCatalogSnapshot, "status" | "catalog">): boolean {
  return status === "error" || (status === "ready" && !catalog?.models.length);
}

/**
 * 「开始对话」不可用的原因：只解释缺工作区 / 缺模型这类需要用户另行操作的前置条件；
 * 草稿为空、提交中、模型目录仍在加载都是显而易见或短暂的，保持安静（返回 null）。
 */
export function startBlockedReason({ hasWorkspaces, selectedWorkspaceId, modelMissing, modelReady }: {
  hasWorkspaces: boolean;
  selectedWorkspaceId: number | null;
  modelMissing: boolean;
  modelReady: boolean;
}): string | null {
  if (!selectedWorkspaceId) return hasWorkspaces ? "先选择工作区" : "先添加工作区";
  if (!modelReady && modelMissing) return "先配置模型";
  return null;
}

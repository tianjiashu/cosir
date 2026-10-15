import type { ModelCatalogSnapshot } from "@/lib/model-catalog";

/**
 * 新对话页「能否开始」的前置条件判定：工作区与模型。
 *
 * 纯函数，只把工作区目录与模型目录的状态翻译成引导与提示需要的结论，
 * 不读取存储也不触发加载；首次使用引导与「开始对话」按钮的不可用原因共用这一份判断。
 */

/** loading 时步骤保持未完成但不给出操作，避免模型目录尚未返回就催用户去配置。 */
export type ModelSetupState = "loading" | "ready" | "empty" | "error";

export function modelSetupState({ status, catalog }: Pick<ModelCatalogSnapshot, "status" | "catalog">): ModelSetupState {
  if (status === "error") return "error";
  if (status !== "ready") return "loading";
  return catalog && catalog.models.length > 0 ? "ready" : "empty";
}

/**
 * 是否展示首次使用引导：缺工作区或缺模型时出现，两项都满足后消失。
 * 工作区目录首次加载期间 workspaces 为空不代表没有工作区，此时不据此展示。
 */
export function shouldShowFirstRunChecklist({ workspacesLoading, hasWorkspaces, modelState }: {
  workspacesLoading: boolean;
  hasWorkspaces: boolean;
  modelState: ModelSetupState;
}): boolean {
  return (!workspacesLoading && !hasWorkspaces) || modelState === "empty" || modelState === "error";
}

/**
 * 「开始对话」不可用的原因：只解释缺工作区 / 缺模型这类需要用户另行操作的前置条件；
 * 草稿为空、提交中、模型目录仍在加载都是显而易见或短暂的，保持安静（返回 null）。
 */
export function startBlockedReason({ hasWorkspaces, selectedWorkspaceId, modelState, modelReady }: {
  hasWorkspaces: boolean;
  selectedWorkspaceId: number | null;
  modelState: ModelSetupState;
  modelReady: boolean;
}): string | null {
  if (!selectedWorkspaceId) return hasWorkspaces ? "先选择工作区" : "先添加工作区";
  if (!modelReady && (modelState === "empty" || modelState === "error")) return "先配置模型";
  return null;
}

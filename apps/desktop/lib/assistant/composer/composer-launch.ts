import { useAuiState } from "@assistant-ui/react";

import type { TransportRunLaunch, TransportState } from "@/lib/assistant/contract";
import { useActiveRunLaunch } from "@/lib/assistant/run-launch";
import { readRuntimeTransportState } from "@/lib/assistant/transport/transport-state-operations";
import { useModelCatalog, type ModelCatalogSnapshot } from "@/lib/model/catalog";
import {
  launchSelection,
  resolveModelSelection,
  useModelSelectionStore,
  type ModelSelection,
  type ModelSelections,
  type ModelSelectionScope,
  type SessionLaunchFacts,
} from "@/lib/model/model-selection-store";

/**
 * 会话输入框「这次发送用哪个发起配置、现在能不能发」的唯一结论。
 *
 * 选择器显示、发送按钮与回车的门槛、提交时的最后一道检查都读它，不各自推导：以前选择器与提交各调一遍回落链，
 * 选择器知道模型目录在加载或加载失败，发送却不知道，于是按钮可点、点了才以「请先选择模型」拒绝。
 *
 * - 运行期锁定（活动 Run）：用它冻结的配置，不依赖模型目录；配置里没有模型（已删除）时不能发。冻结的模型不在
 *   目录里但 id 还在时照常发出——后端把消息留在队列里等用户换模型，「停止后重新选择」只是选择器上的提示。
 * - 未锁定：按回落链（`resolveModelSelection`）在目录里选；目录还在加载、加载失败或为空时不能发。
 */
export type ComposerLaunch = {
  /** 活动 Run 期间配置锁定（控件只读）。 */
  locked: boolean;
} & (
  /** 能发：这次发送用的配置。 */
  | { selection: ModelSelection; blocker: null }
  /** 不能发：原因。 */
  | { selection: null; blocker: ComposerLaunchBlocker }
);

export type ComposerLaunchBlocker = "catalog_loading" | "catalog_error" | "no_model" | "locked_model_deleted";

/** 不能发的说明：发送按钮的提示与提交被拒的文案共用。 */
export const COMPOSER_LAUNCH_BLOCKER_TEXT: Record<ComposerLaunchBlocker, string> = {
  catalog_loading: "模型还在加载，稍后再发送",
  catalog_error: "模型加载失败，在模型选择处重试后再发送",
  no_model: "请先配置模型后再发送",
  locked_model_deleted: "当前运行的模型配置已删除，请停止后重新选择",
};

/** 回落链里取自会话快照的两项发起配置（见 `resolveModelSelection`）。 */
export function sessionLaunchFacts(state: TransportState | null): SessionLaunchFacts {
  return { lastLaunch: state?.activity?.launch ?? null, inheritedLaunch: state?.fork?.initialLaunch ?? null };
}

/**
 * 纯推导。`runLaunch` 是活动 Run 冻结的配置（`activeRunLaunch`，不在运行期为 null），`facts` 是回落链取自会话
 * 快照的两项；提交时从快照现取，输入框里由 {@link useComposerLaunch} 订阅。
 */
export function resolveComposerLaunch({ catalog, selections, scope, runLaunch, facts }: {
  catalog: ModelCatalogSnapshot;
  selections: ModelSelections;
  scope: ModelSelectionScope;
  runLaunch: TransportRunLaunch | null;
  facts: SessionLaunchFacts;
}): ComposerLaunch {
  if (runLaunch !== null) {
    const selection = launchSelection(runLaunch);
    return selection === null
      ? { selection, locked: true, blocker: "locked_model_deleted" }
      : { selection, locked: true, blocker: null };
  }
  if (catalog.status === "loading") return { selection: null, locked: false, blocker: "catalog_loading" };
  if (catalog.status === "error" || catalog.catalog === null) return { selection: null, locked: false, blocker: "catalog_error" };
  const selection = resolveModelSelection(catalog.catalog.models, selections, scope, facts);
  return selection === null ? { selection, locked: false, blocker: "no_model" } : { selection, locked: false, blocker: null };
}

/** 在 Assistant runtime 内订阅 {@link resolveComposerLaunch} 的输入；每次渲染现算（几次查找，不缓存）。 */
export function useComposerLaunch(scope: ModelSelectionScope): ComposerLaunch {
  const catalog = useModelCatalog();
  const sessions = useModelSelectionStore((store) => store.sessions);
  const workspaces = useModelSelectionStore((store) => store.workspaces);
  const runLaunch = useActiveRunLaunch();
  const lastLaunch = useAuiState((aui) => readRuntimeTransportState(aui.thread.state)?.activity?.launch ?? null);
  const inheritedLaunch = useAuiState((aui) => readRuntimeTransportState(aui.thread.state)?.fork?.initialLaunch ?? null);
  return resolveComposerLaunch({
    catalog,
    selections: { sessions, workspaces },
    scope,
    runLaunch,
    facts: { lastLaunch, inheritedLaunch },
  });
}

import { useCallback, useSyncExternalStore } from "react";

import { confirmDiscard } from "@/lib/confirm/confirm-store";
import { isDraftTab, useWorkbenchStore } from "./store";
import type { WorkbenchDraftTab, WorkbenchTab } from "./types";

/**
 * 标签是否有未保存的草稿：两类草稿会话（子 Agent / Team）的 store 都暴露 `dirty`，这里统一订阅，
 * 不为此另立接口；非草稿标签恒为 false。判定不依赖标签是否激活或面板是否挂载。
 */
export function useWorkbenchTabDirty(tab: WorkbenchTab | undefined): boolean {
  const store = tab && isDraftTab(tab) ? tab.session.store : undefined;
  const subscribe = useCallback((notify: () => void) => store ? store.subscribe(() => notify()) : () => undefined, [store]);
  const getDirty = () => store?.getState().dirty ?? false;
  return useSyncExternalStore(subscribe, getDirty, getDirty);
}

/**
 * 工具卡片的「已保存」：标签开着时从会话实时派生（工具草稿一开始就是未保存内容，只有保存才会变干净）；
 * 关闭后读取关闭时记下的标记。
 */
export function useDraftSaved(workspaceId: number | null, toolCallId: string): boolean {
  const tab = useWorkbenchStore((state) => workspaceId === null ? undefined
    : state.sessions[workspaceId]?.tabs.find((candidate): candidate is WorkbenchDraftTab => isDraftTab(candidate) && candidate.toolCallId === toolCallId));
  const stamped = useWorkbenchStore((state) => workspaceId !== null && Boolean(state.sessions[workspaceId]?.savedDraftIds.includes(toolCallId)));
  const dirty = useWorkbenchTabDirty(tab);
  return tab ? !dirty : stamped;
}

/**
 * 关闭单个标签前的确认：只有未保存的草稿才询问，文案点名草稿，同时开着多个草稿时用户分得清在关哪个。
 * 标签栏与草稿面板自带的关闭按钮共用这一处；返回 true 表示可以关闭。
 */
export async function confirmCloseWorkbenchTab(tab: WorkbenchTab): Promise<boolean> {
  if (!isDraftTab(tab) || !tab.session.store.getState().dirty) return true;
  return confirmDiscard({
    title: "关闭配置草稿？",
    description: `「${tab.title}」尚未保存，关闭后修改会丢失。`,
    confirmLabel: "放弃并关闭",
  });
}

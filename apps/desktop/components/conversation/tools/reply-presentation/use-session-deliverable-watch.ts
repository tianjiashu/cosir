import { useSyncExternalStore } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import type { TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import { workspaceDeliverablePaths } from "@/lib/assistant/turn/deliverable-layout";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { useWatchedPaths } from "@/lib/workspace-files/file-watch-store";

const keys = new WeakMap<TransportState, string>();

/**
 * 登记集合的键（排序后的路径 JSON）。按快照对象缓存：流式增量每帧都换快照，但路径集合只在出现新交付物时才变，
 * 返回同一个字符串，`useSyncExternalStore` 就不触发重渲染，监听也不重新登记、不重连。
 */
export function deliverableWatchKey(state: TransportState): string {
  let key = keys.get(state);
  if (key === undefined) {
    key = JSON.stringify(workspaceDeliverablePaths(state.messages));
    keys.set(state, key);
  }
  return key;
}

/**
 * 会话订阅界面（主对话、工作台的子 Agent 只读线程）登记本会话工作区内交付物的路径。登记的是会话级集合，不按卡片
 * 登记：虚拟列表滚出即卸载，按卡片登记会让集合抖动、反复重连并整体失效文件查询。
 *
 * 工作区取引用桥的当前工作区，与交付物卡片现读文件信息（`useDeliverableFiles`）同一来源：监听的与读取的必须是
 * 同一个工作区，否则文件变了，卡片读的那份查询不会失效。
 */
export function useSessionDeliverableWatch(store: TransportFrameStore) {
  const workspaceId = useReferenceBridge()?.workspaceId ?? null;
  const key = useSyncExternalStore(store.subscribe, () => deliverableWatchKey(store.getSnapshot().state));
  useWatchedPaths(workspaceId, JSON.parse(key) as string[]);
}

import { useSyncExternalStore } from "react";

import type { TransportDeliverable, TransportState } from "@/lib/assistant/contract";
import type { TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { useWatchedPaths } from "@/lib/workspace-files/file-watch-store";

const keys = new WeakMap<readonly TransportDeliverable[], string>();

/**
 * 登记集合的键（工作区内交付文件的路径，排序去重后的 JSON）。取自 snapshot 顶层 `deliverables`：它是完整历史的
 * 会话清单，覆盖已加载消息里的交付卡，也覆盖侧栏「产出」里没加载进窗口的早期条目——两处现读的都必须被监听，
 * 否则文件变了、那份查询不会失效。目录与工作区外的条目不现读，也不登记。
 *
 * 按清单数组缓存：流式增量每帧都换快照，清单不变时返回同一个字符串，`useSyncExternalStore` 就不触发重渲染，
 * 监听也不重新登记、不重连。
 */
export function deliverableWatchKey(state: TransportState): string {
  let key = keys.get(state.deliverables);
  if (key === undefined) {
    const paths = new Set<string>();
    for (const deliverable of state.deliverables)
      if (deliverable.type === "file" && deliverable.workspacePath !== null) paths.add(deliverable.workspacePath);
    key = JSON.stringify([...paths].sort());
    keys.set(state.deliverables, key);
  }
  return key;
}

/**
 * 会话订阅界面（主对话、工作台的子 Agent 只读线程）登记本会话工作区内交付物的路径。登记的是会话级集合，不按卡片
 * 登记：虚拟列表滚出即卸载，按卡片登记会让集合抖动、反复重连并整体失效文件查询。
 *
 * 工作区取引用桥的当前工作区，与交付物现读文件信息（`useDeliverableFile`）同一来源：监听的与读取的必须是
 * 同一个工作区，否则文件变了，读的那份查询不会失效。
 */
export function useSessionDeliverableWatch(store: TransportFrameStore) {
  const workspaceId = useReferenceBridge()?.workspaceId ?? null;
  const key = useSyncExternalStore(store.subscribe, () => deliverableWatchKey(store.getSnapshot().state));
  useWatchedPaths(workspaceId, JSON.parse(key) as string[]);
}

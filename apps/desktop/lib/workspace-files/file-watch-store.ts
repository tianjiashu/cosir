import { useEffect } from "react";
import { create } from "zustand";

/** 工作区 id → 路径 → 登记次数。 */
type Registrations = Record<number, Record<string, number>>;

type FileWatchState = {
  byWorkspace: Registrations;
  /** 登记一组路径，返回撤销函数；同一路径被多方登记时按次数计，最后一方撤销才移除。 */
  register: (workspaceId: number, paths: readonly string[]) => () => void;
};

function adjust(registrations: Registrations, workspaceId: number, paths: readonly string[], delta: 1 | -1): Registrations {
  const current = { ...registrations[workspaceId] };
  for (const path of paths) {
    const count = (current[path] ?? 0) + delta;
    if (count > 0) current[path] = count;
    else delete current[path];
  }
  const next = { ...registrations };
  if (Object.keys(current).length) next[workspaceId] = current;
  else delete next[workspaceId];
  return next;
}

/**
 * 工作区文件监听的登记：「文件变了」的信号归工作区，各界面只声明要看哪些路径。
 *
 * 登记方有三类：文件面板（展开的目录、标签所在目录）、Markdown 引用的本地图片、会话订阅界面（已加载消息里工作区内
 * 的交付物）。`WorkspaceFileWatches`（挂在 AppLayout）为每个至少有一项登记的工作区维持一条监听连接，登记清零即
 * 关闭；后端订阅时对文件取父目录、对目录取自身，前端不必换算。只是界面状态，不持久化。
 */
export const useFileWatchStore = create<FileWatchState>()((set) => ({
  byWorkspace: {},
  register: (workspaceId, paths) => {
    const unique = [...new Set(paths)];
    if (!unique.length) return () => {};
    set((state) => ({ byWorkspace: adjust(state.byWorkspace, workspaceId, unique, 1) }));
    return () => set((state) => ({ byWorkspace: adjust(state.byWorkspace, workspaceId, unique, -1) }));
  },
}));

/**
 * 在组件挂载期间登记路径。按排序后的路径集合比较：集合不变时不重新登记，调用方每次渲染传新数组也不会让连接抖动。
 * `workspaceId` 为 null（没有工作区）时不登记。
 */
export function useWatchedPaths(workspaceId: number | null, paths: readonly string[]) {
  const register = useFileWatchStore((state) => state.register);
  const key = JSON.stringify([...new Set(paths)].sort());
  useEffect(() => {
    if (workspaceId === null) return;
    return register(workspaceId, JSON.parse(key) as string[]);
  }, [register, workspaceId, key]);
}

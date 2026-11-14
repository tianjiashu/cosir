import { create } from "zustand";
import { persist } from "zustand/middleware";

import { preferenceStorage } from "@/lib/storage/preference-storage";

type LastWorkspace = { workspaceId: number | null };

type LastWorkspaceState = LastWorkspace & { remember: (workspaceId: number) => void };

/** 读入时收敛：只接受正整数 id；它是否仍存在由目录加载时核对（`use-workspace-catalog`）。 */
export function normalizeLastWorkspace(raw: unknown): LastWorkspace {
  const workspaceId = typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>).workspaceId : undefined;
  return { workspaceId: Number.isInteger(workspaceId) && (workspaceId as number) > 0 ? workspaceId as number : null };
}

/**
 * 最近工作区：启动时恢复上次选中的工作区，属于导航记忆，只由工作区目录（`use-workspace-catalog`）
 * 读写；界面尺寸偏好在 `lib/layout/preferences.ts`。
 */
export const useLastWorkspaceStore = create<LastWorkspaceState>()(persist(
  (set) => ({
    workspaceId: null,
    remember: (workspaceId) => set({ workspaceId }),
  }),
  {
    name: "cosir:last-workspace",
    storage: preferenceStorage<LastWorkspace>(),
    partialize: ({ workspaceId }) => ({ workspaceId }),
    merge: (persisted, current) => ({ ...current, ...normalizeLastWorkspace(persisted) }),
  },
));

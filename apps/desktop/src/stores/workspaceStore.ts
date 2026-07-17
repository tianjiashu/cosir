/**
 * 工作区状态管理（Zustand）。
 *
 * 管理 workspace 列表、折叠态和当前活跃 workspace。
 *
 * @module stores/workspaceStore
 */

import { create } from "zustand";
import type { WorkspaceRecord } from "@shared/workspace";

/** 工作区 Store 状态接口。 */
interface WorkspaceState {
  /** 已加载的工作区列表。 */
  workspaces: WorkspaceRecord[];
  /** 当前选中的工作区标识。 */
  activeWorkspaceId: string | null;
  /** 已折叠的工作区标识集合。 */
  collapsedWorkspaceIds: Set<string>;
}

/** 工作区 Store 动作接口。 */
interface WorkspaceActions {
  /** 批量替换工作区列表。 */
  setWorkspaces: (workspaces: WorkspaceRecord[]) => void;
  /** 添加或替换一个工作区。 */
  upsertWorkspace: (workspace: WorkspaceRecord) => void;
  /** 删除一个工作区。 */
  removeWorkspace: (workspaceId: string) => void;
  /** 设置当前活跃工作区。 */
  setActiveWorkspace: (workspaceId: string | null) => void;
  /** 切换工作区折叠状态。 */
  toggleWorkspaceCollapsed: (workspaceId: string) => void;
}

/**
 * 工作区 Zustand Store 实例。
 *
 * @returns Zustand hook；组件调用后可读取 workspace 列表、活跃 workspace 和折叠态。
 *
 * @throws 不主动抛出异常。
 *
 * @sideeffect action 会更新前端内存状态；不会直接发起 HTTP、IPC 或写日志。
 */
export const useWorkspaceStore = create<WorkspaceState & WorkspaceActions>((set) => ({
  workspaces: [],
  activeWorkspaceId: null,
  collapsedWorkspaceIds: new Set<string>(),

  setWorkspaces: (workspaces) => {
    set((state) => ({
      workspaces,
      activeWorkspaceId: state.activeWorkspaceId ?? workspaces[0]?.workspace_id ?? null,
    }));
  },

  upsertWorkspace: (workspace) => {
    set((state) => ({
      workspaces: [workspace, ...state.workspaces.filter((item) => item.workspace_id !== workspace.workspace_id)],
      activeWorkspaceId: state.activeWorkspaceId ?? workspace.workspace_id,
    }));
  },

  removeWorkspace: (workspaceId) => {
    set((state) => ({
      workspaces: state.workspaces.filter((item) => item.workspace_id !== workspaceId),
      activeWorkspaceId: state.activeWorkspaceId === workspaceId ? null : state.activeWorkspaceId,
    }));
  },

  setActiveWorkspace: (workspaceId) => {
    set({ activeWorkspaceId: workspaceId });
  },

  toggleWorkspaceCollapsed: (workspaceId) => {
    set((state) => {
      const next = new Set(state.collapsedWorkspaceIds);
      if (next.has(workspaceId)) {
        next.delete(workspaceId);
      } else {
        next.add(workspaceId);
      }
      return { collapsedWorkspaceIds: next };
    });
  },
}));

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
  /** 当前选中的工作区标识（后端 int 主键，前端以 number 承载）。 */
  activeWorkspaceId: number | null;
  /** 已折叠的工作区标识集合（键为后端 int 主键）。 */
  collapsedWorkspaceIds: Set<number>;
}

/** 工作区 Store 动作接口。 */
interface WorkspaceActions {
  /** 批量替换工作区列表。 */
  setWorkspaces: (workspaces: WorkspaceRecord[]) => void;
  /** 添加或替换一个工作区。 */
  upsertWorkspace: (workspace: WorkspaceRecord) => void;
  /** 删除一个工作区。 */
  removeWorkspace: (workspaceId: number) => void;
  /** 设置当前活跃工作区。 */
  setActiveWorkspace: (workspaceId: number | null) => void;
  /** 切换工作区折叠状态。 */
  toggleWorkspaceCollapsed: (workspaceId: number) => void;
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
  collapsedWorkspaceIds: new Set<number>(),

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
    set((state) => {
      const nextWorkspaces = state.workspaces.filter((item) => item.workspace_id !== workspaceId);
      // 删除后若正好是当前活跃工作区，自动切到剩余列表中的第一个；
      // 列表已空则置 null（由调用方引导新建工作区）。
      const nextActive =
        state.activeWorkspaceId === workspaceId
          ? nextWorkspaces[0]?.workspace_id ?? null
          : state.activeWorkspaceId;
      return { workspaces: nextWorkspaces, activeWorkspaceId: nextActive };
    });
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

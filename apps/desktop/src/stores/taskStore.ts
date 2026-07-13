/**
 * 任务与会话状态管理（Zustand）。
 *
 * 管理：
 * - 当前会话信息
 * - 任务列表
 * - 当前活跃任务
 * - 任务状态变更动作
 *
 * 派生 UI 状态（如运行状态标签）由 store 数据计算，不单独冗余存储。
 *
 * @module stores/taskStore
 */

import { create } from "zustand";
import type { TaskRecord } from "@shared/task";
import type { TaskStatus } from "@shared/task";

/** 任务 Store 的状态接口。 */
interface TaskState {
  /** 当前会话 ID（如有）。 */
  currentSessionId: string | null;
  /** 已加载的任务列表。 */
  tasks: TaskRecord[];
  /** 当前正在查看/交互的任务 ID（不一定在运行中）。 */
  activeTaskId: string | null;
}

/** 任务 Store 的动作接口。 */
interface TaskActions {
  /** 设置当前会话 ID。 */
  setCurrentSession: (sessionId: string) => void;
  /** 添加一个新任务到列表。 */
  addTask: (task: TaskRecord) => void;
  /** 更新任务状态（根据 task_id 匹配并替换）。 */
  updateTask: (taskId: string, updates: Partial<TaskRecord>) => void;
  /** 设置当前活跃任务。 */
  setActiveTask: (taskId: string | null) => void;
  /** 清空所有任务数据。 */
  clearTasks: () => void;
  /**
   * 根据任务 ID 获取任务记录。
   * @param taskId - 查找的目标任务 ID。
   * @returns 匹配的任务记录，未找到返回 undefined。
   */
  getTaskById: (taskId: string) => TaskRecord | undefined;
}

/**
 * 任务 Zustand Store 实例。
 *
 * 使用 zustand create API，支持 React 组件外直接调用
 * （如 service 层更新后同步 store）。
 */
export const useTaskStore = create<TaskState & TaskActions>((set, get) => ({
  // --- 初始状态 ---
  currentSessionId: null,
  tasks: [],
  activeTaskId: null,

  // --- 动作 ---

  setCurrentSession: (sessionId: string) => {
    set({ currentSessionId: sessionId });
  },

  addTask: (task: TaskRecord) => {
    set((state) => ({
      tasks: [task, ...state.tasks],
      activeTaskId: state.activeTaskId ?? task.task_id,
    }));
  },

  updateTask: (taskId: string, updates: Partial<TaskRecord>) => {
    set((state) => ({
      tasks: state.tasks.map((t) =>
        t.task_id === taskId ? { ...t, ...updates } : t,
      ),
    }));
  },

  setActiveTask: (taskId: string | null) => {
    set({ activeTaskId: taskId });
  },

  clearTasks: () => {
    set({ tasks: [], activeTaskId: null });
  },

  getTaskById: (taskId: string) => {
    return get().tasks.find((t) => t.task_id === taskId);
  },
}));

// ---------- 派生选择器 ----------

/**
 * 获取当前活跃任务的完整记录。
 * @returns 活跃任务对象或 undefined。
 */
export const selectActiveTask = (state: TaskState): TaskRecord | undefined => {
  if (!state.activeTaskId) return undefined;
  return state.tasks.find((t) => t.task_id === state.activeTaskId);
};

/**
 * 获取当前活跃任务的运行状态。
 * @returns 任务状态字符串或 null。
 */
export const selectActiveTaskStatus = (state: TaskState): TaskStatus | null => {
  if (!state.activeTaskId) return null;
  return state.tasks.find((t) => t.task_id === state.activeTaskId)?.status ?? null;
};

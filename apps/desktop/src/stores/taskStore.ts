/**
 * 任务容器状态管理（Zustand）。
 *
 * 管理：
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
  /** 已加载的任务列表。 */
  tasks: TaskRecord[];
  /** 当前正在查看/交互的任务 ID（不一定在运行中）。 */
  activeTaskId: string | null;
  /** 当前活跃轮次 ID。 */
  activeTurnId: string | null;
}

/** 任务 Store 的动作接口。 */
interface TaskActions {
  /** 批量替换任务列表。 */
  setTasks: (tasks: TaskRecord[]) => void;
  /** 添加一个新任务到列表。 */
  addTask: (task: TaskRecord) => void;
  /** 用正式任务替换临时任务。 */
  replaceTask: (temporaryTaskId: string, task: TaskRecord) => void;
  /** 删除一个任务。 */
  removeTask: (taskId: string) => void;
  /** 更新任务状态（根据 task_id 匹配并替换）。 */
  updateTask: (taskId: string, updates: Partial<TaskRecord>) => void;
  /** 设置当前活跃任务。 */
  setActiveTask: (taskId: string | null, turnId?: string | null) => void;
  /** 设置当前活跃轮次。 */
  setActiveTurn: (turnId: string | null) => void;
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
  tasks: [],
  activeTaskId: null,
  activeTurnId: null,

  // --- 动作 ---

  setTasks: (tasks: TaskRecord[]) => {
    set({ tasks });
  },

  addTask: (task: TaskRecord) => {
    set((state) => ({
      tasks: [task, ...state.tasks.filter((item) => item.task_id !== task.task_id)],
      activeTaskId: state.activeTaskId ?? task.task_id,
      activeTurnId: state.activeTurnId ?? task.latest_turn_id,
    }));
  },

  replaceTask: (temporaryTaskId: string, task: TaskRecord) => {
    set((state) => ({
      tasks: [task, ...state.tasks.filter((item) => item.task_id !== temporaryTaskId && item.task_id !== task.task_id)],
      activeTaskId: state.activeTaskId === temporaryTaskId ? task.task_id : state.activeTaskId,
      activeTurnId: state.activeTaskId === temporaryTaskId ? task.latest_turn_id ?? null : state.activeTurnId,
    }));
  },

  removeTask: (taskId: string) => {
    set((state) => ({
      tasks: state.tasks.filter((item) => item.task_id !== taskId),
      activeTaskId: state.activeTaskId === taskId ? null : state.activeTaskId,
      activeTurnId: state.activeTaskId === taskId ? null : state.activeTurnId,
    }));
  },

  updateTask: (taskId: string, updates: Partial<TaskRecord>) => {
    set((state) => ({
      tasks: state.tasks.map((t) =>
        t.task_id === taskId ? { ...t, ...updates } : t,
      ),
    }));
  },

  setActiveTask: (taskId: string | null, turnId?: string | null) => {
    const task = taskId ? get().tasks.find((item) => item.task_id === taskId) : undefined;
    set({ activeTaskId: taskId, activeTurnId: turnId ?? task?.latest_turn_id ?? null });
  },

  setActiveTurn: (turnId: string | null) => {
    set({ activeTurnId: turnId });
  },

  clearTasks: () => {
    set({ tasks: [], activeTaskId: null, activeTurnId: null });
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
  const task = state.tasks.find((t) => t.task_id === state.activeTaskId);
  return task?.execution_status ?? task?.status ?? null;
};

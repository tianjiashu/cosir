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
import { useEventStore } from "@/stores/eventStore";
import { logWarn, logInfo } from "@/lib/logger";

/**
 * 上次活跃任务 ID 的本地持久化键。
 *
 * 用于进入应用时自动恢复上次正在查看的任务，避免每次都需要手动点击侧栏任务
 * 才能加载中央会话区。仅持久化真实任务 ID（"temp-" 前缀的临时任务不持久化）。
 */
const ACTIVE_TASK_STORAGE_KEY = "coding-agent.activeTaskId";

/**
 * 判断任务 ID 是否为可持久化的真实任务（排除 "temp-" 前缀的临时任务）。
 *
 * @param taskId - 待判断的任务 ID。
 * @returns 为真实任务 ID 时返回 true。
 */
function isPersistableTask(taskId: string | null): taskId is string {
  return !!taskId && !taskId.startsWith("temp-");
}

/**
 * 读取本地持久化的上次活跃任务 ID。
 *
 * localStorage 不可用（如隐私模式）时记录 WARN 日志后仍返回 null，不影响主流程。
 *
 * @returns 持久化的任务 ID；无有效值时返回 null。
 */
export function loadPersistedActiveTaskId(): string | null {
  try {
    const value = localStorage.getItem(ACTIVE_TASK_STORAGE_KEY);
    return value && isPersistableTask(value) ? value : null;
  } catch (err) {
    logWarn("读取持久化活跃任务失败，降级为无恢复态", { module: "taskStore", error: err instanceof Error ? err.message : String(err) });
    return null;
  }
}

/**
 * 持久化活跃任务 ID 到本地存储。
 *
 * 仅持久化真实任务 ID；临时任务或 null 时清除持久化值。localStorage 不可用时
 * 记录 WARN 日志，不抛错、不影响内存状态与交互。
 *
 * @param taskId - 待持久化的任务 ID；临时任务或 null 时清除持久化值。
 */
function persistActiveTaskId(taskId: string | null): void {
  try {
    if (isPersistableTask(taskId)) {
      localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, taskId);
    } else {
      localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY);
    }
  } catch (err) {
    logWarn("持久化活跃任务失败", { module: "taskStore", task_id: taskId, error: err instanceof Error ? err.message : String(err) });
  }
}

/**
 * 统一设置活跃任务并同步持久化。
 *
 * 这是「凡改 activeTaskId 必同步 localStorage」这一不变量的唯一出口，避免
 * ``setTasks`` 等分支用 ``set`` 直写绕过持久化导致内存态与持久化态撕裂。
 *
 * @param set - Zustand 的 set 函数。
 * @param taskId - 目标活跃任务 ID（可为 null 表示清空）。
 * @param turnId - 可选的目标活跃轮次 ID；缺省时按任务 latest_turn_id 推导。
 */
function applyActiveTask(
  set: (partial: Partial<TaskState>) => void,
  taskId: string | null,
  turnId?: string | null,
): void {
  const task = taskId ? useTaskStore.getState().tasks.find((item) => item.task_id === taskId) : undefined;
  set({
    activeTaskId: taskId,
    activeTurnId: turnId ?? task?.latest_turn_id ?? null,
  });
  persistActiveTaskId(taskId);
}

/** 任务 Store 的状态接口。 */
interface TaskState {
  /** 已加载的任务列表。 */
  tasks: TaskRecord[];
  /** 当前正在查看/交互的任务 ID（不一定在运行中）。 */
  activeTaskId: string | null;
  /** 当前活跃轮次 ID。 */
  activeTurnId: string | null;
  /** 当前选中的 Agent 标识（用于创建任务/轮次时传入后端）。 */
  selectedAgentId: string;
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
  /** 设置当前选中的 Agent 标识。 */
  setSelectedAgentId: (agentId: string) => void;
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
  // 进入应用时优先恢复上次活跃任务（持久化于 localStorage）；无记录时为 null。
  activeTaskId: loadPersistedActiveTaskId(),
  activeTurnId: null,
  selectedAgentId: "developer",

  // --- 动作 ---

  setTasks: (tasks: TaskRecord[]) => {
    // 任务列表整体替换时，若持久化的活跃任务仍在列表中则保持，否则回退到首项。
    // 回退分支必须同步持久化，避免内存态已切走而 localStorage 残留脏值。
    const persistedId = useTaskStore.getState().activeTaskId;
    const keptTask = persistedId ? tasks.find((task) => task.task_id === persistedId) : undefined;
    const fallbackTask = keptTask ?? tasks[0] ?? null;
    set({ tasks });
    applyActiveTask(set, fallbackTask?.task_id ?? null, fallbackTask?.latest_turn_id ?? null);
    if (persistedId && !keptTask) {
      logInfo("持久化活跃任务已不存在，回退到任务列表首项", {
        module: "taskStore",
        persisted_task_id: persistedId,
        fallback_task_id: fallbackTask?.task_id ?? null,
      });
    }
  },

  addTask: (task: TaskRecord) => {
    set((state) => ({
      tasks: [task, ...state.tasks.filter((item) => item.task_id !== task.task_id)],
    }));
    // 若尚无活跃任务，新任务自动成为活跃任务；经统一入口同步持久化。
    if (!get().activeTaskId) {
      applyActiveTask(set, task.task_id, task.latest_turn_id);
    }
  },

  replaceTask: (temporaryTaskId: string, task: TaskRecord) => {
    set((state) => ({
      tasks: [task, ...state.tasks.filter((item) => item.task_id !== temporaryTaskId && item.task_id !== task.task_id)],
    }));
    // 临时任务转正：活跃 ID 从 temp-x 变为真实 ID 时，必须同步持久化，
    // 否则新建任务重启后无法恢复（与 setActiveTask 共用同一不变式出口）。
    if (get().activeTaskId === temporaryTaskId) {
      applyActiveTask(set, task.task_id, task.latest_turn_id ?? null);
    }
  },

  removeTask: (taskId: string) => {
    const wasActive = get().activeTaskId === taskId;
    set((state) => ({
      tasks: state.tasks.filter((item) => item.task_id !== taskId),
    }));
    // 删除的是当前活跃任务时同步清除持久化，避免下次启动恢复到一个已删除的任务。
    if (wasActive) {
      applyActiveTask(set, null);
    }
    // 任务移除即意味着其历史事件缓存失效，同步清除以避免幽灵 timeline。
    useEventStore.getState().invalidateTask(taskId);
  },

  updateTask: (taskId: string, updates: Partial<TaskRecord>) => {
    set((state) => ({
      tasks: state.tasks.map((t) =>
        t.task_id === taskId ? { ...t, ...updates } : t,
      ),
    }));
  },

  setActiveTask: (taskId: string | null, turnId?: string | null) => {
    // 经统一入口设置，确保持久化与内存态一致（临时任务/清空时自动清除持久化）。
    applyActiveTask(set, taskId, turnId);
  },

  setActiveTurn: (turnId: string | null) => {
    set({ activeTurnId: turnId });
  },

  setSelectedAgentId: (agentId: string) => {
    set({ selectedAgentId: agentId });
  },

  clearTasks: () => {
    set({ tasks: [], activeTurnId: null });
    // 清空活跃任务须同步清除持久化，避免下次启动恢复到一个已不存在的任务。
    applyActiveTask(set, null);
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

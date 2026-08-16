/**
 * 任务容器状态管理（Zustand）。
 *
 * 管理：
 * - 按 task_id 索引的任务实体缓存（tasksById）
 * - 按 workspace_id 分组的任务列表缓存（tasksByWorkspaceId）
 * - 已加载工作区集合（loadedWorkspaceIds）：加载态与数据态正交，不靠数组是否存在兼职
 * - 当前活跃任务
 * - 任务状态变更动作
 *
 * 加载态契约：``loadedWorkspaceIds`` 含某 workspaceId 表示该 workspace 的任务列表已
 * 经从后端拉取（即便为空也是 ``[]``）；不含表示尚未加载（惰性填充）。侧栏据此判定是否
 * 需补拉，新增任务在未加载分组时不得伪造 ``[task]`` 以免永久跳过真实拉取。
 *
 * 派生 UI 状态（如运行状态标签）由 store 数据计算，不单独冗余存储。
 * 首屏活跃任务恢复由 App 层（resumeAttempted 分支）单点负责，本 store 不内嵌恢复编排，
 * 以确保「展开 workspace 永不意外切走中央对话」这一不变量。
 *
 * @module stores/taskStore
 */

import { create } from "zustand";
import type { TaskRecord } from "@shared/task";
import type { TaskStatus } from "@shared/task";
import { useEventStore } from "@/stores/eventStore";
import { logWarn } from "@/lib/logger";

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
 * 各分支用 ``set`` 直写绕过持久化导致内存态与持久化态撕裂。任务记录经
 * ``get().tasksById`` 查找，未加载分组中查不到不影响选中与主对话区渲染。
 *
 * @param set - Zustand 的 set 函数。
 * @param taskId - 目标活跃任务 ID（可为 null 表示清空）。
 * @param turnId - 可选的目标活跃轮次 ID；缺省时置为 null，真实轮次 ID 由调用方
 *   （useTask）经 turnsByTask 显式设置。
 */
function applyActiveTask(
  set: (partial: Partial<TaskState>) => void,
  taskId: string | null,
  turnId?: string | null,
): void {
  set({
    activeTaskId: taskId,
    activeTurnId: turnId ?? null,
  });
  persistActiveTaskId(taskId);
}

/** 任务 Store 的状态接口。 */
interface TaskState {
  /** 按 task_id 索引的任务实体缓存；主对话区与顶部状态读取该事实源，不依赖 workspace 列表是否已加载。 */
  tasksById: Record<string, TaskRecord>;
  /** 按 workspace_id 分组缓存的任务列表，每个 workspace 独立维护自身任务。 */
  tasksByWorkspaceId: Record<string, TaskRecord[]>;
  /** 已向后端拉取过任务列表的工作区 ID 集合；不含表示尚未加载（惰性填充）。 */
  loadedWorkspaceIds: Set<string>;
  /** 当前正在查看/交互的任务 ID（不一定在运行中）。 */
  activeTaskId: string | null;
  /** 当前活跃轮次 ID。 */
  activeTurnId: string | null;
  /** 当前选中的 Agent 标识（用于创建任务/轮次时传入后端）。 */
  selectedAgentId: string;
}

/** 任务 Store 的动作接口。 */
interface TaskActions {
  /** 写入指定 workspace 的任务列表并标记该 workspace 已加载（惰性加载/刷新用）。 */
  setWorkspaceTasks: (workspaceId: string, tasks: TaskRecord[]) => void;
  /** 判断指定 workspace 是否已从后端加载过任务列表。 */
  isWorkspaceLoaded: (workspaceId: string) => boolean;
  /** 添加一个新任务到所属 workspace 分组。 */
  addTask: (task: TaskRecord) => void;
  /** 用正式任务替换临时任务。 */
  replaceTask: (temporaryTaskId: string, task: TaskRecord) => void;
  /** 删除一个任务（跨分组清理）。 */
  removeTask: (taskId: string) => void;
  /** 更新任务状态（根据 task_id 跨分组匹配并替换）。 */
  updateTask: (taskId: string, updates: Partial<TaskRecord>) => void;
  /** 设置当前活跃任务。 */
  setActiveTask: (taskId: string | null, turnId?: string | null) => void;
  /** 设置当前活跃轮次。 */
  setActiveTurn: (turnId: string | null) => void;
  /** 设置当前选中的 Agent 标识。 */
  setSelectedAgentId: (agentId: string) => void;
  /** 清空所有任务数据。 */
  clearTasks: () => void;
  /** 清理指定 workspace 的分组（删除工作区时级联）。 */
  clearWorkspaceTasks: (workspaceId: string) => void;
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
  tasksById: {},
  tasksByWorkspaceId: {},
  loadedWorkspaceIds: new Set(),
  // 进入应用时优先恢复上次活跃任务（持久化于 localStorage）；无记录时为 null。
  activeTaskId: loadPersistedActiveTaskId(),
  activeTurnId: null,
  selectedAgentId: "developer",

  // --- 动作 ---

  setWorkspaceTasks: (workspaceId: string, tasks: TaskRecord[]) => {
    // 纯粹的分组写入并标记已加载。首屏活跃任务恢复由 App 层（resumeAttempted 分支）
    // 单点负责，本 action 不内嵌任何恢复/回退编排，以从结构上杜绝「展开 workspace
    // 意外切走中央对话」这一本次需求明确禁止的行为。
    const incomingTaskIds = new Set(tasks.map((task) => task.task_id));
    const removedTaskIds = Object.values(get().tasksById)
      .filter((task) => task.workspace_id === workspaceId && !incomingTaskIds.has(task.task_id))
      .map((task) => task.task_id);
    const activeTaskId = get().activeTaskId;
    const activeTaskRemoved = activeTaskId !== null && removedTaskIds.includes(activeTaskId);

    set((state) => {
      const loaded = new Set(state.loadedWorkspaceIds);
      loaded.add(workspaceId);
      const tasksById = { ...state.tasksById };
      for (const taskId of removedTaskIds) {
        delete tasksById[taskId];
      }
      for (const task of tasks) {
        tasksById[task.task_id] = task;
      }
      return {
        tasksById,
        tasksByWorkspaceId: { ...state.tasksByWorkspaceId, [workspaceId]: tasks },
        loadedWorkspaceIds: loaded,
        ...(activeTaskRemoved ? { activeTaskId: null, activeTurnId: null } : {}),
      };
    });
    if (activeTaskRemoved) {
      persistActiveTaskId(null);
    }
    for (const taskId of removedTaskIds) {
      useEventStore.getState().invalidateTask(taskId);
    }
  },

  isWorkspaceLoaded: (workspaceId: string) => {
    return get().loadedWorkspaceIds.has(workspaceId);
  },

  addTask: (task: TaskRecord) => {
    // 仅当该 workspace 已加载时才把新任务插入分组；未加载分组不接收注入，
    // 否则会把「未加载（undefined）」伪造成「已加载且只有 1 条」，导致后续真实拉取
    // 被 isWorkspaceLoaded 判定跳过（见 useWorkspaceTaskLazyLoad）。未加载时仅更新
    // 活跃任务，真实列表交给随后的 ensureLoaded 拉取。
    set((state) => {
      const partial: Partial<TaskState> = {
        tasksById: { ...state.tasksById, [task.task_id]: task },
      };
      if (state.loadedWorkspaceIds.has(task.workspace_id)) {
        const group = state.tasksByWorkspaceId[task.workspace_id] ?? [];
        partial.tasksByWorkspaceId = {
          ...state.tasksByWorkspaceId,
          [task.workspace_id]: [task, ...group.filter((item) => item.task_id !== task.task_id)],
        };
      }
      return partial;
    });
    // 若尚无活跃任务，新任务自动成为活跃任务；活跃轮次由调用方（useTask）
    // 经 turnsByTask 显式设置，此处不持有任务级 turn 推导。
    if (!get().activeTaskId) {
      applyActiveTask(set, task.task_id, null);
    }
  },

  replaceTask: (temporaryTaskId: string, task: TaskRecord) => {
    // 同 addTask：仅已加载分组接收注入，避免伪造加载态。
    set((state) => {
      const tasksById = { ...state.tasksById };
      delete tasksById[temporaryTaskId];
      tasksById[task.task_id] = task;
      const partial: Partial<TaskState> = { tasksById };
      if (state.loadedWorkspaceIds.has(task.workspace_id)) {
        const group = state.tasksByWorkspaceId[task.workspace_id] ?? [];
        partial.tasksByWorkspaceId = {
          ...state.tasksByWorkspaceId,
          [task.workspace_id]: [
            task,
            ...group.filter((item) => item.task_id !== temporaryTaskId && item.task_id !== task.task_id),
          ],
        };
      }
      return partial;
    });
    // 临时任务转正：活跃 ID 从 temp-x 变为真实 ID 时，必须同步持久化，
    // 否则新建任务重启后无法恢复（与 setActiveTask 共用同一不变式出口）。
    // 真实轮次 ID 由调用方（useTask）显式经 setActiveTask 设置，此处传 null。
    if (get().activeTaskId === temporaryTaskId) {
      applyActiveTask(set, task.task_id, null);
    }
  },

  removeTask: (taskId: string) => {
    const wasActive = get().activeTaskId === taskId;
    set((state) => {
      const tasksById = { ...state.tasksById };
      delete tasksById[taskId];
      const next: Record<string, TaskRecord[]> = {};
      for (const [wsId, list] of Object.entries(state.tasksByWorkspaceId)) {
        next[wsId] = list.filter((item) => item.task_id !== taskId);
      }
      return { tasksById, tasksByWorkspaceId: next };
    });
    // 删除的是当前活跃任务时同步清除持久化，避免下次启动恢复到一个已删除的任务。
    if (wasActive) {
      applyActiveTask(set, null);
    }
    // 任务移除即意味着其历史事件缓存失效，同步清除以避免幽灵 timeline。
    useEventStore.getState().invalidateTask(taskId);
  },

  updateTask: (taskId: string, updates: Partial<TaskRecord>) => {
    set((state) => {
      const currentTask = state.tasksById[taskId];
      const tasksById = currentTask
        ? { ...state.tasksById, [taskId]: { ...currentTask, ...updates } }
        : state.tasksById;
      const next: Record<string, TaskRecord[]> = {};
      for (const [wsId, list] of Object.entries(state.tasksByWorkspaceId)) {
        next[wsId] = list.map((t) => (t.task_id === taskId ? { ...t, ...updates } : t));
      }
      return { tasksById, tasksByWorkspaceId: next };
    });
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
    set({ tasksById: {}, tasksByWorkspaceId: {}, loadedWorkspaceIds: new Set(), activeTurnId: null });
    // 清空活跃任务须同步清除持久化，避免下次启动恢复到一个已不存在的任务。
    applyActiveTask(set, null);
  },

  clearWorkspaceTasks: (workspaceId: string) => {
    // 级联清理：被删工作区下的任务事件缓存失效，且若活跃任务恰在该工作区则清空活跃态，
    // 避免 activeTaskId 悬空指向已删工作区的任务（与 removeTask 行为对齐，不泄漏到组件层）。
    // 活跃任务归属判定直接基于 activeTaskId 的 workspace（经 getTaskById），不依赖分组是否
    // 加载——未展开过的 workspace 也照样能清掉幽灵活跃态与事件缓存。
    const groupedTaskIds = (get().tasksByWorkspaceId[workspaceId] ?? []).map((task) => task.task_id);
    const entityTaskIds = Object.values(get().tasksById)
      .filter((task) => task.workspace_id === workspaceId)
      .map((task) => task.task_id);
    const removedTaskIds = [...new Set([...groupedTaskIds, ...entityTaskIds])];
    const activeId = get().activeTaskId;
    const activeTask = activeId ? get().tasksById[activeId] : undefined;
    const wasActiveInWorkspace = activeTask?.workspace_id === workspaceId;
    set((state) => {
      const next = { ...state.tasksByWorkspaceId };
      delete next[workspaceId];
      const tasksById = { ...state.tasksById };
      for (const taskId of removedTaskIds) {
        delete tasksById[taskId];
      }
      const loaded = new Set(state.loadedWorkspaceIds);
      loaded.delete(workspaceId);
      return { tasksById, tasksByWorkspaceId: next, loadedWorkspaceIds: loaded };
    });
    for (const taskId of removedTaskIds) {
      useEventStore.getState().invalidateTask(taskId);
    }
    if (wasActiveInWorkspace) {
      applyActiveTask(set, null);
    }
  },

  getTaskById: (taskId: string) => {
    return get().tasksById[taskId];
  },
}));

// ---------- 派生选择器 ----------

/**
 * 获取当前活跃任务的完整记录。
 * @returns 活跃任务对象或 undefined。
 */
export const selectActiveTask = (state: TaskState): TaskRecord | undefined => {
  if (!state.activeTaskId) return undefined;
  return state.tasksById[state.activeTaskId];
};

/**
 * 获取当前活跃任务的运行状态。
 * @returns 任务状态字符串或 null。
 */
export const selectActiveTaskStatus = (state: TaskState): TaskStatus | null => {
  if (!state.activeTaskId) return null;
  const task = state.tasksById[state.activeTaskId];
  return task?.execution_status ?? task?.status ?? null;
};

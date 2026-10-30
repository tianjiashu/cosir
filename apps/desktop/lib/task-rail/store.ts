import { create } from "zustand";
import { persist } from "zustand/middleware";

/** 会话侧栏的卡片 id；新增卡片时在这里登记，并在卡片注册表（`components/task-rail/cards.tsx`）补齐实现。 */
export const TASK_RAIL_CARD_IDS = ["background-tasks"] as const;
export type RailCardId = (typeof TASK_RAIL_CARD_IDS)[number];

type TaskRailPreferences = {
  open: boolean;
  order: RailCardId[];
  collapsed: Partial<Record<RailCardId, boolean>>;
};

type TaskRailState = TaskRailPreferences & {
  setOpen: (open: boolean) => void;
  setCollapsed: (id: RailCardId, collapsed: boolean) => void;
  setOrder: (order: RailCardId[]) => void;
};

function isCardId(value: unknown): value is RailCardId {
  return TASK_RAIL_CARD_IDS.includes(value as RailCardId);
}

/**
 * 把读到的偏好收敛成合法值：丢掉已不存在的卡片、去重，再把新增的卡片补到末尾，
 * 以后加卡片不会打乱老用户排好的顺序。持久化数据来自 localStorage（可能被手改或来自旧版本），
 * 这是它唯一的入口校验。
 */
export function normalizeTaskRailPreferences(raw: unknown): TaskRailPreferences {
  const value = typeof raw === "object" && raw !== null ? raw as Record<string, unknown> : {};
  const stored = Array.isArray(value.order) ? [...new Set(value.order.filter(isCardId))] : [];
  const collapsedRaw = typeof value.collapsed === "object" && value.collapsed !== null ? value.collapsed as Record<string, unknown> : {};
  return {
    open: value.open === true,
    order: [...stored, ...TASK_RAIL_CARD_IDS.filter((id) => !stored.includes(id))],
    collapsed: Object.fromEntries(Object.entries(collapsedRaw).filter(([id, folded]) => isCardId(id) && typeof folded === "boolean")),
  };
}

/**
 * 会话侧栏的界面偏好（开关、卡片顺序、各卡片是否折叠）：全局一份，所有 Task 共用，不是任务事实。
 * 页头按钮与侧栏都要实时读到同一份，所以是响应式 store，经 zustand `persist`（默认存 localStorage）落盘；
 * 存储不可用（隐私模式等）时只在本次会话内生效。
 */
export const useTaskRailStore = create<TaskRailState>()(persist(
  (set) => ({
    ...normalizeTaskRailPreferences(undefined),
    setOpen: (open) => set({ open }),
    setCollapsed: (id, collapsed) => set((state) => ({ collapsed: { ...state.collapsed, [id]: collapsed } })),
    setOrder: (order) => set({ order }),
  }),
  {
    name: "cosir:task-rail",
    partialize: ({ open, order, collapsed }) => ({ open, order, collapsed }),
    merge: (persisted, current) => ({ ...current, ...normalizeTaskRailPreferences(persisted) }),
  },
));

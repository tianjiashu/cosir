import { create } from "zustand";
import { persist } from "zustand/middleware";

import { callKey, useDisclosureStore } from "@/lib/assistant/turn/disclosure-store";

/** 会话侧栏的卡片 id；新增卡片时在这里登记，并在卡片注册表（`components/conversation-aside/aside-card-registry.tsx`）补齐实现。 */
export const CONVERSATION_ASIDE_CARD_IDS = ["background-tasks"] as const;
export type AsideCardId = (typeof CONVERSATION_ASIDE_CARD_IDS)[number];

type ConversationAsidePreferences = {
  open: boolean;
  order: AsideCardId[];
  collapsed: Partial<Record<AsideCardId, boolean>>;
};

type ConversationAsideState = ConversationAsidePreferences & {
  /** 对话视口放不下固定侧栏；`ConversationAsidePanel` 按视口写入，页头开关据此判断在开关哪一种。 */
  floating: boolean;
  /** 窄屏浮层是否打开，不落盘：浮层盖在对话上，每次都由用户点开，宽屏的常驻偏好不带过来。 */
  floatOpen: boolean;
  /** 待滚动到的看板条目；展开已在 `revealBackground` 里写入。 */
  revealCall: { taskId: number; callId: string } | null;
  /** 用户从时间线定位到看板：打开侧栏与卡片并展开这一条。 */
  revealBackground: (taskId: number, callId: string) => void;
  /** 条目已定位；一次性意图不留在 store 里，否则条目重新挂载时会再展开一次。 */
  consumeReveal: () => void;
  setFloating: (floating: boolean) => void;
  setVisible: (visible: boolean) => void;
  setCollapsed: (id: AsideCardId, collapsed: boolean) => void;
  setOrder: (order: AsideCardId[]) => void;
};

function isCardId(value: unknown): value is AsideCardId {
  return CONVERSATION_ASIDE_CARD_IDS.includes(value as AsideCardId);
}

/** 只接受当前完整偏好；结构不符时整体恢复默认值，不迁移卡片顺序或保留旧字段。 */
export function normalizeConversationAsidePreferences(raw: unknown): ConversationAsidePreferences {
  const defaults: ConversationAsidePreferences = { open: true, order: [...CONVERSATION_ASIDE_CARD_IDS], collapsed: {} };
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return defaults;
  const value = raw as Record<string, unknown>;
  if (Object.keys(value).length !== 3 || typeof value.open !== "boolean"
    || !Array.isArray(value.order) || value.order.length !== CONVERSATION_ASIDE_CARD_IDS.length
    || !value.order.every(isCardId) || new Set(value.order).size !== value.order.length
    || typeof value.collapsed !== "object" || value.collapsed === null || Array.isArray(value.collapsed)
    || !Object.entries(value.collapsed).every(([id, folded]) => isCardId(id) && typeof folded === "boolean")) return defaults;
  return { open: value.open, order: value.order, collapsed: value.collapsed };
}

/**
 * 会话侧栏的界面偏好（开关、卡片顺序、各卡片是否折叠）：全局一份，所有 Task 共用，不是任务事实。
 * 页头按钮与侧栏都要实时读到同一份，所以是响应式 store，经 zustand `persist`（默认存 localStorage）落盘；
 * 存储不可用（隐私模式等）时只在本次会话内生效。
 */
export const useConversationAsideStore = create<ConversationAsideState>()(persist(
  (set) => ({
    ...normalizeConversationAsidePreferences(undefined),
    floating: false,
    floatOpen: false,
    revealCall: null,
    revealBackground: (taskId, callId) => {
      useDisclosureStore.getState().set(taskId, callKey(callId), true);
      set((state) => ({ open: true, floatOpen: true, collapsed: { ...state.collapsed, "background-tasks": false }, revealCall: { taskId, callId } }));
    },
    consumeReveal: () => set({ revealCall: null }),
    setFloating: (floating) => set({ floating }),
    setVisible: (visible) => set((state) => (state.floating ? { floatOpen: visible } : { open: visible })),
    setCollapsed: (id, collapsed) => set((state) => ({ collapsed: { ...state.collapsed, [id]: collapsed } })),
    setOrder: (order) => set({ order }),
  }),
  {
    name: "cosir:conversation-aside",
    partialize: ({ open, order, collapsed }) => ({ open, order, collapsed }),
    merge: (persisted, current) => ({ ...current, ...normalizeConversationAsidePreferences(persisted) }),
  },
));

export const selectConversationAsideVisible = ({ floating, floatOpen, open }: ConversationAsideState): boolean => (floating ? floatOpen : open);

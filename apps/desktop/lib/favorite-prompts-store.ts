import { create } from "zustand";
import { persist } from "zustand/middleware";

import { preferenceStorage } from "@/lib/storage/preference-storage";

export type FavoritePrompt = {
  id: string;
  title: string;
  content: string;
};

type FavoritePrompts = { prompts: readonly FavoritePrompt[] };

type FavoritePromptsState = FavoritePrompts & {
  /** 新建（不带 id）或按 id 改写一条；名称与内容由调用方先去掉首尾空白并确认非空。 */
  save: (prompt: { id?: string; title: string; content: string }) => void;
  remove: (id: string) => void;
};

function isFavoritePrompt(value: unknown): value is FavoritePrompt {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.id === "string"
    && record.id.length > 0
    && typeof record.title === "string"
    && record.title.trim().length > 0
    && typeof record.content === "string"
    && record.content.trim().length > 0;
}

/** 读入时收敛：只留结构完整的提示词，其余丢弃。 */
export function normalizeFavoritePrompts(raw: unknown): FavoritePrompts {
  const prompts = typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>).prompts : undefined;
  return { prompts: Array.isArray(prompts) ? prompts.filter(isFavoritePrompt) : [] };
}

/**
 * 本机常用提示词：用户可复用的提示词列表，按界面显示顺序保存。不是会话事实，不记录提示词内容日志，
 * 也不负责插入输入框（`favorite-prompt-page.tsx`）。
 */
export const useFavoritePromptsStore = create<FavoritePromptsState>()(persist(
  (set) => ({
    prompts: [],
    save: ({ id, title, content }) => set((state) => ({
      prompts: id === undefined
        ? [...state.prompts, { id: crypto.randomUUID(), title, content }]
        : state.prompts.map((prompt) => (prompt.id === id ? { ...prompt, title, content } : prompt)),
    })),
    remove: (id) => set((state) => ({ prompts: state.prompts.filter((prompt) => prompt.id !== id) })),
  }),
  {
    name: "cosir:favorite-prompts",
    storage: preferenceStorage<FavoritePrompts>(),
    partialize: ({ prompts }) => ({ prompts }),
    merge: (persisted, current) => ({ ...current, ...normalizeFavoritePrompts(persisted) }),
  },
));

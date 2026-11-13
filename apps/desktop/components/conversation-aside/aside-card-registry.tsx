import type { ComponentType } from "react";

import { CONVERSATION_ASIDE_CARD_IDS, type AsideCardId } from "@/lib/conversation-aside/conversation-aside-store";

import { BackgroundTasksActions, BackgroundTasksBody, BackgroundTasksSummary, useBackgroundTasksHasContent } from "./background-tasks-card";

type AsideCardDefinition = {
  title: string;
  /** 这张卡此刻有没有可说的；没有时卡片不渲染，所有卡片都没有时侧栏与页头入口一起不出现。 */
  useHasContent: (taskId: number | null) => boolean;
  /** 折叠时标题行右侧的一句摘要。 */
  Summary: ComponentType<{ taskId: number }>;
  /** 标题行右侧的卡片级操作，折叠与展开时都在；没有可做的事时自己返回 null。 */
  Actions?: ComponentType<{ taskId: number }>;
  Body: ComponentType<{ taskId: number }>;
};

/**
 * 会话侧栏的卡片注册表：每个登记的卡片 id 恰好一个实现，漏写或多写编译期报错。
 * 新增卡片：在 `CONVERSATION_ASIDE_CARD_IDS` 登记 id，再在这里补实现；外壳（折叠、拖拽、偏好、显隐）不用动。
 */
export const CONVERSATION_ASIDE_CARDS: Record<AsideCardId, AsideCardDefinition> = {
  "background-tasks": { title: "后台任务", useHasContent: useBackgroundTasksHasContent, Summary: BackgroundTasksSummary, Actions: BackgroundTasksActions, Body: BackgroundTasksBody },
};

/**
 * 侧栏有没有任何内容：侧栏本身与页头入口共用这一个判定。逐个调用各卡片的 `useHasContent`——卡片 id 是
 * 模块常量、顺序固定，每次渲染调用顺序一致；用 `||` 拼在后面而不是短路，保证每个 hook 每次都被调用。
 */
export function useConversationAsideHasContent(taskId: number | null): boolean {
  let hasContent = false;
  for (const id of CONVERSATION_ASIDE_CARD_IDS) hasContent = CONVERSATION_ASIDE_CARDS[id].useHasContent(taskId) || hasContent;
  return hasContent;
}

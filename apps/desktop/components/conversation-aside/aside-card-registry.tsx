import type { ComponentType } from "react";

import type { AsideCardId } from "@/lib/conversation-aside/conversation-aside-store";

import { BackgroundTasksActions, BackgroundTasksBody, BackgroundTasksSummary } from "./background-tasks-card";

type AsideCardDefinition = {
  title: string;
  /** 折叠时标题行右侧的一句摘要。 */
  Summary: ComponentType<{ taskId: number }>;
  /** 标题行右侧的卡片级操作，折叠与展开时都在；没有可做的事时自己返回 null。 */
  Actions?: ComponentType<{ taskId: number }>;
  Body: ComponentType<{ taskId: number }>;
};

/**
 * 会话侧栏的卡片注册表：每个登记的卡片 id 恰好一个实现，漏写或多写编译期报错。
 * 新增卡片：在 `CONVERSATION_ASIDE_CARD_IDS` 登记 id，再在这里补实现；外壳（折叠、拖拽、偏好）不用动。
 */
export const CONVERSATION_ASIDE_CARDS: Record<AsideCardId, AsideCardDefinition> = {
  "background-tasks": { title: "后台任务", Summary: BackgroundTasksSummary, Actions: BackgroundTasksActions, Body: BackgroundTasksBody },
};

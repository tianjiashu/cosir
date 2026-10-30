import type { ComponentType } from "react";

import type { RailCardId } from "@/lib/task-rail/store";

import { BackgroundTasksBody, BackgroundTasksSummary } from "./background-tasks-card";

type RailCardDefinition = {
  title: string;
  /** 折叠时标题行右侧的一句摘要，展开时也保留。 */
  Summary: ComponentType<{ taskId: number }>;
  Body: ComponentType<{ taskId: number }>;
};

/**
 * 会话侧栏的卡片注册表：每个登记的卡片 id 恰好一个实现，漏写或多写编译期报错。
 * 新增卡片：在 `TASK_RAIL_CARD_IDS` 登记 id，再在这里补实现；外壳（折叠、拖拽、偏好）不用动。
 */
export const TASK_RAIL_CARDS: Record<RailCardId, RailCardDefinition> = {
  "background-tasks": { title: "后台任务", Summary: BackgroundTasksSummary, Body: BackgroundTasksBody },
};

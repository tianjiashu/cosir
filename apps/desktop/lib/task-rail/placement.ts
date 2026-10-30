/** 固定成一列时侧栏的宽度：展开的后台条目就是对话里那张工具卡片（含终端输出），再窄放不下。 */
export const TASK_RAIL_WIDTH = 340;
/** 侧栏固定后对话区至少保留的阅读宽度；不够就改为浮层，不再挤对话。 */
const CONVERSATION_MIN_WIDTH = 640;

export type TaskRailPlacement = "docked" | "floating";

/**
 * 侧栏的摆放方式。`rowWidth` 是「对话 + 侧栏」整行的宽度：量整行而不是对话区，
 * 否则侧栏一固定对话就变窄、随即又判为浮起，来回抖动。打开工作台与窗口变窄都只是让这一行变窄。
 */
export function taskRailPlacement(rowWidth: number): TaskRailPlacement {
  return rowWidth - TASK_RAIL_WIDTH >= CONVERSATION_MIN_WIDTH ? "docked" : "floating";
}

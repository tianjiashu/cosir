/** 固定成一列时侧栏的宽度：展开的后台条目就是对话里那张工具卡片（含终端输出），再窄放不下。 */
export const TASK_RAIL_WIDTH = 340;
/** 侧栏固定后对话区至少保留的阅读宽度；不够就改为浮层，不再挤对话。 */
const CONVERSATION_MIN_WIDTH = 640;

export type TaskRailPlacement = "docked" | "floating";

/**
 * 侧栏的摆放方式。`viewportWidth` 是对话视口（滚动容器）的宽度：侧栏是视口里的附注，固定与否都不改变
 * 视口宽度，所以按它判断不会来回抖动。打开右侧面板与窗口变窄都只是让视口变窄。
 */
export function taskRailPlacement(viewportWidth: number): TaskRailPlacement {
  return viewportWidth - TASK_RAIL_WIDTH >= CONVERSATION_MIN_WIDTH ? "docked" : "floating";
}

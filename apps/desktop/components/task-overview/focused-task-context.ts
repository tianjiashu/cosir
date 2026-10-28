import { createContext, useContext } from "react";

/**
 * 工作台里「任务概览」跟随的 Task：由 AppLayout 按当前聚焦的 Task 提供。概览标签每个工作区只有
 * 一个，内容随聚焦 Task 切换，所以不把 taskId 存进标签。
 */
export const FocusedTaskIdContext = createContext<number | null>(null);

export function useFocusedTaskId(): number | null {
  return useContext(FocusedTaskIdContext);
}

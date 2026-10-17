import { createContext } from "react";

/** 活动文本阅读器注册查找动作；标签与正文共享快捷键边界，不监听整个窗口。 */
export const ReadingSearchContext = createContext<
  ((action: (() => void) | null) => void) | null
>(null);

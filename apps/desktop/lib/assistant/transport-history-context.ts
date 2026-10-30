import { createContext, useContext } from "react";

/**
 * 当前对话「往上翻一页更早历史」的入口，由创建 Transport runtime 的一方在 `AssistantRuntimeProvider`
 * 旁提供（对话页与工作台只读阅读器各一处）。虚拟列表滚到接近顶部时调用；没有提供方时不翻页。
 */
export const TransportHistoryContext = createContext<(() => Promise<void>) | null>(null);

export function useLoadEarlierHistory(): (() => Promise<void>) | null {
  return useContext(TransportHistoryContext);
}

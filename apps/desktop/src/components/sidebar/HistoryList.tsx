/**
 * 历史会话入口组件（占位）。
 *
 * 第一版仅展示静态按钮，点击无实际功能。
 *
 * @module components/sidebar/HistoryList
 */

import { History } from "lucide-react";

/**
 * HistoryList 历史会话入口占位组件。
 *
 * 展示历史会话入口按钮，后续接入会话历史列表。
 */
export function HistoryList() {
  return (
    <div className="px-2 py-1">
      <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground hover:bg-accent/50 hover:text-accent-foreground transition-colors">
        <History className="h-4 w-4" />
        聊天
      </button>
    </div>
  );
}

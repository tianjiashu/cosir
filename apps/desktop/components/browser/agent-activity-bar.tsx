import { BotIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * 网页上方的一行：Agent 正在运行时提醒用户「这个页面可能被它操作」，正在执行时说「正在操作」。只做提示、不放按钮：
 * 要停下 Agent 用对话里已有的停止或取消。是否在运行只读会话状态推送（`lib/session-status/`），是否正在执行读宿主
 * 的标签事实（`agentBusy`），不另立第二个来源。
 */
export function AgentActivityBar({ busy }: { busy: boolean }) {
  return (
    <div role="status" aria-live="polite" className={cn("mx-2 mb-1 flex items-center gap-2 rounded-md px-3 py-1.5 text-xs", busy ? "bg-info/10 text-foreground" : "bg-muted/60 text-muted-foreground")}>
      <BotIcon className={cn("size-3.5 shrink-0", busy && "text-info animate-pulse")} aria-hidden="true" />
      <span className="truncate">{busy ? "Agent 正在操作这个页面" : "Agent 正在运行，可能会操作这个页面"}</span>
    </div>
  );
}

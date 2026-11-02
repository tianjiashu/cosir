import { MessagePrimitive } from "@assistant-ui/react";
import { BotIcon } from "lucide-react";
import { useCallback, type FC } from "react";
import { useNavigate } from "react-router-dom";

import { MarkdownText } from "@/components/markdown-text";
import { getTask } from "@/lib/api/workspaces";
import type { TransportMessageSource } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { useWorkbenchActions } from "@/lib/workbench/context";

/** 侧栏里的对话 Task；其余（委派子 Agent、Team 节点）只能在工作台里读。 */
const SIDEBAR_TASK_TYPES = new Set(["user", "fork"]);

/**
 * 打开 Agent 消息的来源 Task：侧栏对话按路由跳转，子 Agent 与 Team 节点在工作台打开。
 *
 * 来源是哪种 Task 在点击时向后端读一次，不随消息下发：快照只带 `taskId` 与显示名，任务类型
 * 留在它唯一的事实源里。
 */
function useOpenMessageSource() {
  const navigate = useNavigate();
  const workbench = useWorkbenchActions();
  return useCallback(async (source: TransportMessageSource) => {
    try {
      const task = await getTask(source.taskId);
      if (SIDEBAR_TASK_TYPES.has(task.task_type)) {
        navigate(`/tasks/${source.taskId}`);
      } else {
        workbench?.openAgent({ taskId: source.taskId, title: source.name });
      }
    } catch (error) {
      void frontendLog("WARNING", "agent_message_source_open_failed", "打开 Agent 消息的来源失败", {
        data: { taskId: source.taskId },
        error,
      });
      notify.error("无法打开来源", { error, fallback: "来源任务可能已被删除" });
    }
  }, [navigate, workbench]);
}

/**
 * 另一个 Agent 交给本 Task 的消息（委派、追加消息、Team 节点输入）：不是用户说的话。
 *
 * 靠左、不画成用户气泡，标出发送方并可跳到来源；不可编辑重跑（后端同样拒绝）。正文是纯文本，按
 * Markdown 排版，不解析用户输入里的附件胶囊。
 */
export const AgentInboundMessage: FC<{ source: TransportMessageSource }> = ({ source }) => {
  const openSource = useOpenMessageSource();
  return (
    <MessagePrimitive.Root data-role="agent-message" className="flex flex-col items-start px-2">
      <div className="flex max-w-[85%] flex-col items-start gap-1.5">
        <button
          type="button"
          className="text-muted-foreground hover:text-foreground flex items-center gap-1 text-xs"
          onClick={() => void openSource(source)}
        >
          <BotIcon className="size-3.5" aria-hidden />
          <span>来自 {source.name}</span>
        </button>
        <div className="bg-surface text-foreground rounded-2xl px-4 py-2.5 text-sm leading-relaxed wrap-break-word">
          <MessagePrimitive.Parts>{({ part }) => (
            part.type === "text" ? <MarkdownText status={part.status} /> : null
          )}</MessagePrimitive.Parts>
        </div>
      </div>
    </MessagePrimitive.Root>
  );
};

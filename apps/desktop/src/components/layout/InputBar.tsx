/**
 * 底部输入区（InputBar）。
 *
 * 展示：
 * - 纯文本输入框
 * - 发送按钮
 * - 模型/模式选择下拉（占位）
 * - 权限状态指示器（占位）
 * - 语音入口占位
 * - "+" 附件入口（占位，点击提示"即将上线"）
 *
 * 第一版仅支持纯文本输入。发送时按当前上下文创建任务或追加 turn，并启动 SSE 流。
 *
 * @module components/layout/InputBar
 */

import { useState, useCallback } from "react";
import { Send, Mic, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { AgentSelector } from "@/components/chat/AgentSelector";
import { logInfo, logError } from "@/lib/logger";
import { useTask } from "@/hooks/useTask";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { beginClientTrace, endClientTrace } from "@/services/tracePropagation";

/**
 * InputBar 底部输入区组件。
 *
 * 固定在主会话区底部，包含文本输入和操作按钮。
 * 发送按钮仅在输入框非空且不在加载中时激活。
 */
export function InputBar() {
  const [inputValue, setInputValue] = useState("");
  const { createTask, createTurn, operation } = useTask();
  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  const streamingTurnId = useTurnStore((s) => s.streamingTurnId);
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const selectedAgentId = useTaskStore((s) => s.selectedAgentId);
  const setSelectedAgentId = useTaskStore((s) => s.setSelectedAgentId);
  const trimmedInput = inputValue.trim();
  const canSend = Boolean(trimmedInput) && !operation.loading && !streamingTurnId && Boolean(activeTaskId || activeWorkspaceId);

  /** 处理发送操作：按当前上下文创建任务或追加 turn，并启动 SSE 监听。 */
  const handleSend = useCallback(async () => {
    if (!canSend) return;

    const text = trimmedInput;
    beginClientTrace();
    logInfo("用户提交任务输入", { module: "InputBar", input_preview: text.slice(0, 100) });

    try {
      let succeeded = false;
      if (activeTaskId) {
        succeeded = await createTurn(text);
      } else if (activeWorkspaceId) {
        succeeded = await createTask(text, activeWorkspaceId);
      }
      if (succeeded) {
        setInputValue("");
      }
    } catch (err) {
      // useTask 内部已通过 logError 记录详细错误并更新 operation.error，
      // 此处仅做防御性日志，避免吞掉异常上下文。
      logError("handleSend: 发送任务输入抛出未捕获异常", err instanceof Error ? err : new Error(String(err)), {
        module: "InputBar",
        input_preview: text.slice(0, 100),
      });
    } finally {
      endClientTrace();
    }
  }, [activeTaskId, activeWorkspaceId, canSend, createTask, createTurn, trimmedInput]);

  /** 处理键盘事件：Enter 发送，Shift+Enter 换行。 */
  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="border-t border-border bg-background px-4 py-3">
      {/* 输入区域容器 */}
      <div className="mx-auto flex max-w-3xl items-end gap-2">
        {/* 左侧附件按钮（占位） */}
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="icon" className="h-9 w-9 shrink-0 text-muted-foreground">
                <Plus className="h-4 w-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top">
              <p>附件上传（即将上线）</p>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        {/* 文本输入框 */}
        <div className="relative flex-1">
          <Input
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={activeTaskId ? "给 Agent 下达任务..." : "输入任务内容开始对话..."}
            className="min-h-[36px] pr-24 resize-none"
          />

          {/* 右侧内嵌按钮组 */}
          <div className="absolute right-1.5 top-1/2 flex -translate-y-1/2 items-center gap-0.5">
            {/* Agent 选择器 */}
            <AgentSelector
              value={selectedAgentId}
              onChange={setSelectedAgentId}
              className="h-7"
            />

            {/* 语音入口（占位） */}
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="ghost" size="icon" className="h-7 w-7 text-muted-foreground">
                    <Mic className="h-3.5 w-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="top">
                  <p>语音输入（即将上线）</p>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
        </div>

        {/* 发送按钮 */}
        <Button
          aria-label="发送"
          onClick={handleSend}
          disabled={!canSend}
          size="icon"
          className="h-9 w-9 shrink-0"
        >
          <Send className="h-4 w-4" />
        </Button>
      </div>

    </div>
  );
}

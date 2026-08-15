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
import { Mic, Plus, Send, Square } from "lucide-react";
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
import { useClientTraceStore } from "@/stores/clientTraceStore";
import { PerfTrace } from "@/lib/perf";

/**
 * InputBar 底部输入区组件。
 *
 * 固定在主会话区底部，包含文本输入和操作按钮。
 * 发送按钮仅在输入框非空且不在加载中时激活。
 */
export function InputBar() {
  const [inputValue, setInputValue] = useState("");
  const { createTask, createTurn, cancelTurn, operation } = useTask();
  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  const streamingTurnId = useTurnStore((s) => s.streamingTurnId);
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const selectedAgentId = useTaskStore((s) => s.selectedAgentId);
  const setSelectedAgentId = useTaskStore((s) => s.setSelectedAgentId);
  const trimmedInput = inputValue.trim();
  const canSend = Boolean(trimmedInput) && !operation.loading && !streamingTurnId && Boolean(activeTaskId || activeWorkspaceId);
  const canStop = Boolean(streamingTurnId) && !operation.loading;
  const placeholder = !activeWorkspaceId
    ? "请先选择工作区再开始对话..."
    : activeTaskId
      ? "给 Agent 下达任务..."
      : "输入任务内容开始对话...";

  /** 处理发送操作：按当前上下文创建任务或追加 turn，并启动 SSE 监听。 */
  const handleSend = useCallback(async () => {
    if (!canSend) return;

    const text = trimmedInput;
    beginClientTrace();
    const perf = PerfTrace.startCurrent("user-input-to-render", useClientTraceStore.getState().currentTrace?.traceId);
    logInfo("用户提交任务输入", { module: "InputBar", input_preview: text.slice(0, 100), trace_id: perf.traceId });

    try {
      let succeeded = false;
      if (activeTaskId) {
        perf.mark("handleSend:before-createTurn", { task_id: activeTaskId });
        succeeded = await createTurn(text);
        perf.mark("handleSend:after-createTurn", { task_id: activeTaskId });
      } else if (activeWorkspaceId) {
        perf.mark("handleSend:before-createTask", { workspace_id: activeWorkspaceId });
        succeeded = await createTask(text, activeWorkspaceId);
        perf.mark("handleSend:after-createTask", { workspace_id: activeWorkspaceId });
      }
      if (succeeded) {
        setInputValue("");
      }
      perf.mark("handleSend:done", { succeeded });
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

  /** 处理停止操作：取消当前正在流式运行的 turn。 */
  const handleStop = useCallback(async () => {
    if (!canStop) return;
    beginClientTrace();
    logInfo("用户请求停止当前轮次", { module: "InputBar", turn_id: streamingTurnId });

    try {
      await cancelTurn();
    } catch (err) {
      logError("handleStop: 停止当前轮次抛出未捕获异常", err instanceof Error ? err : new Error(String(err)), {
        module: "InputBar",
        turn_id: streamingTurnId,
      });
    } finally {
      endClientTrace();
    }
  }, [cancelTurn, canStop, streamingTurnId]);

  /** 处理键盘事件：Enter 发送，Shift+Enter 换行；IME 组合输入（中文/日文选词上屏）期间的 Enter 不发送。 */
  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="border-t border-border bg-background px-4 py-3">
      {/* 输入区域容器 */}
      <div className="mx-auto flex max-w-content items-end gap-2">
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

        {/* 文本输入框（与右侧内嵌按钮组同处一个 flex 容器，按钮组自然占位，消除手工预留耦合） */}
        <div className="flex min-w-0 flex-1 items-center gap-1">
          <Input
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            // 36px 为输入框最小高度（单行舒适高度），非通用间距语义，待收敛到 token
            // eslint-disable-next-line tailwind/no-arbitrary-value
            className="min-h-[36px] min-w-0 flex-1 resize-none"
          />

          {/* 右侧内嵌按钮组 */}
          <div className="flex shrink-0 items-center gap-0.5">
            {/* Agent 选择器 */}
            <AgentSelector
              value={selectedAgentId}
              onChange={setSelectedAgentId}
              className="h-7"
            />

            {!streamingTurnId && (
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
            )}
          </div>
        </div>

        {streamingTurnId ? (
          <Button
            aria-label="停止当前轮次"
            onClick={handleStop}
            disabled={!canStop}
            size="icon"
            variant="secondary"
            className="h-9 w-9 shrink-0"
          >
            <Square className="h-4 w-4 fill-current" />
          </Button>
        ) : (
          <Button
            aria-label="发送"
            variant="primary"
            onClick={handleSend}
            disabled={!canSend}
            size="icon"
            className="h-9 w-9 shrink-0"
          >
            <Send className="h-4 w-4" />
          </Button>
        )}
      </div>

    </div>
  );
}

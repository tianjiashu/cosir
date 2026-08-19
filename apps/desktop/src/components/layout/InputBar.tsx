/**
 * 底部输入区（InputBar）。
 *
 * 单一职责：文本输入 + 发送 / 停止 + 上下文占用展示。
 *
 * 具体能力：
 * - 纯文本输入框（含 IME 组合态 Enter 防护）。
 * - 发送按钮（仅在输入框非空且不在加载中且就绪时激活）。
 * - 停止按钮（出现条件：存在 streaming turn）。
 * - 附件占位按钮（即将上线）。
 * - 语音输入占位按钮（即将上线，streaming 期间隐藏）。
 * - 上下文占用状态栏（ContextUsageRing）。
 *
 * 不在职责范围内（已抽离到 `components/chat/TaskHeaderBar.tsx`）：
 * - Agent 选择器（AgentSelector）。
 * - 模型选择器（ModelSelector）。
 * - 模型厂商配置中心（ProviderSettingsDialog）。
 *
 * Agent / Model 选择是 task 维度的元数据，应在 task 创建前/期间就显示——
 * 在 NewTaskPage 顶部（任务创建前）以及 ChatPanel 顶部（追加 turn 前）。
 * InputBar 仅承担「按下发送时的最终值」，事实源由 useTask 同步读取
 * `useTaskStore.selectedAgentId` / `selectedModelName`，不在此原地提供选择入口。
 *
 * 发送前执行模型校验拦截（`useModelSendGuard.guardSend`）：模型未显式选择或
 * 厂商未配置时阻止发送，内联展示拦截原因；拦截规则要求打开配置中心时经
 * `onOpenSettings` 回调联动打开（由宿主在 ChatPanel 顶部的 TaskHeaderBar 上打开）。
 *
 * 按钮置灰语义（方案 §阶段 1.5）：未显式选择模型（selectedModelName=null）时
 * 发送按钮直接 disabled，并 hover 显示 tooltip「请先选择模型」；guardSend 作为
 * 防御兜底仍保留——模型被删（model_missing）/ 厂商 Key 未配置（api_key_missing）
 * 等运行时场景仍由 guardSend 拦截并内联展示原因。
 *
 * @module components/layout/InputBar
 */

import { useState, useCallback, useEffect } from "react";
import { Mic, Plus, Send, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { ContextUsageRing } from "@/components/chat/ContextUsageRing";
import { logInfo, logError, logWarn } from "@/lib/logger";
import { useTask } from "@/hooks/useTask";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { beginClientTrace, endClientTrace } from "@/services/tracePropagation";
import { useClientTraceStore } from "@/stores/clientTraceStore";
import { PerfTrace } from "@/lib/perf";
import { useModelSendGuard } from "@/hooks/useModelSendGuard";

/**
 * InputBar 底部输入区组件。
 *
 * 固定在主会话区底部，包含文本输入和操作按钮。
 * 发送按钮仅在输入框非空且不在加载中时激活。
 *
 * Agent / Model 选择已上移到 TaskHeaderBar——本组件不再持有相关 props 或 state。
 *
 * @param props - 组件属性。
 * @param props.onOpenSettings - 可选回调：发送拦截规则要求打开模型厂商配置中心时调用，
 *   由宿主联动打开 ChatPanel 顶部的 ProviderSettingsDialog。
 */
export function InputBar({ onOpenSettings }: { onOpenSettings?: () => void }) {
  const [inputValue, setInputValue] = useState("");
  const { createTask, createTurn, cancelTurn, operation } = useTask();
  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  // 读取当前 active task 维度下的 streaming turn（按 task 隔离，支持多 task 并发流式）。
  const streamingTurnId = useTurnStore((s) =>
    s.streamingTurnIds[activeTaskId ?? ""] ?? null,
  );
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const trimmedInput = inputValue.trim();
  // 模型未显式选择时禁用发送按钮（方案 §阶段 1.5 / §八：发送按钮 disabled 条件加入
  // `!selectedModelId`）。按钮置灰为主，guardSend 作为防御兜底仍保留——例如模型在
  // 选中后被删除（model_missing）或厂商 Key 未配置（api_key_missing）时，按钮仍可点
  // 但会被 guardSend 在运行时拦截并展示原因。
  const selectedModelName = useTaskStore((s) => s.selectedModelName);
  const hasModelSelected = Boolean(selectedModelName);
  const noModelSelected = !hasModelSelected;
  const canSend =
    Boolean(trimmedInput) &&
    !operation.loading &&
    !streamingTurnId &&
    Boolean(activeTaskId || activeWorkspaceId) &&
    hasModelSelected;
  const canStop = Boolean(streamingTurnId) && !operation.loading;
  // 发送前模型校验拦截提示，展示在输入区下方。
  const [guardMessage, setGuardMessage] = useState<string | null>(null);
  const { guardSend } = useModelSendGuard();
  // 用户切换模型（顶部选择器变更 selectedModelName）后清除拦截提示：
  // 提示「请先选择模型」等已失去意义，避免误导（2026-08-18 无 Auto 语义）。
  useEffect(() => {
    setGuardMessage(null);
  }, [selectedModelName]);
  const placeholder = !activeWorkspaceId
    ? "请先选择工作区再开始对话..."
    : activeTaskId
      ? "给 Agent 下达任务..."
      : "输入任务内容开始对话...";

  /**
   * 处理发送操作：先过模型校验拦截（未选择模型 / 厂商未配置时阻止发送），
   * 通过后按当前上下文创建任务或追加 turn，并启动 SSE 监听。
   */
  const handleSend = useCallback(async () => {
    if (!canSend) return;

    const guard = await guardSend();
    if (!guard.ok) {
      setGuardMessage(guard.block.message);
      if (guard.block.openSettings) {
        onOpenSettings?.();
      }
      // 发送被模型校验拦截是用户请求入口的关键拒绝路径：记录拦截原因与上下文，
      // 便于排查「为什么发不出去、被哪条规则拦的」（可排查日志规范）。
      logWarn("发送被模型校验拦截", {
        module: "InputBar",
        reason: guard.block.reason,
        message: guard.block.message,
        openSettings: guard.block.openSettings,
        hasActiveTask: Boolean(activeTaskId),
        workspace: activeWorkspaceId ?? null,
      });
      return;
    }
    setGuardMessage(null);

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
  }, [activeTaskId, activeWorkspaceId, canSend, createTask, createTurn, guardSend, onOpenSettings, trimmedInput]);

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

        {/* 文本输入框：Agent / Model 选择已上移，右侧内嵌按钮组仅留语音占位 */}
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

          {/* 右侧内嵌按钮组：仅语音占位，Agent / Model 选择器已上移到 TaskHeaderBar */}
          <div className="flex shrink-0 items-center gap-0.5">
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
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex">
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
                </span>
              </TooltipTrigger>
              {/* 未选模型时按钮置灰并提示「请先选择模型」（方案 §阶段 1.5）。
               * 其余置灰原因（无输入/加载中/流式中/无上下文）无业务引导，不展示 tooltip。 */}
              {noModelSelected && (
                <TooltipContent side="top">
                  <p>请先选择模型</p>
                </TooltipContent>
              )}
            </Tooltip>
          </TooltipProvider>
        )}
      </div>

      {/* 上下文占用状态栏：圆环 + token 计数，反映当前任务上下文窗口余量 */}
      <div className="mx-auto mt-1.5 flex max-w-content items-center justify-start">
        <ContextUsageRing />
      </div>

      {/* 发送前模型校验拦截提示（如「请先选择模型」） */}
      {guardMessage && (
        <div className="mx-auto mt-1.5 max-w-content">
          <p className="text-sm text-destructive">{guardMessage}</p>
        </div>
      )}
    </div>
  );
}

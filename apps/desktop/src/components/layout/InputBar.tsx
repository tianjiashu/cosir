/**
 * 底部输入区（InputBar）。
 *
 * 单一职责：文本输入 + 发送 / 停止 + 上下文占用展示。
 *
 * 具体能力：
 * - 纯文本输入框（含 IME 组合态 Enter 防护、长按 Enter 防连发）。
 * - 发送按钮（仅在输入框非空且不在加载中且就绪时激活）。
 * - 停止按钮（出现条件：存在 streaming turn）。
 * - 附件占位按钮（即将上线）。
 * - 语音输入占位按钮（即将上线，streaming 期间隐藏）。
 * - 上下文占用状态栏（ContextUsageRing）。
 * - 跨 task 草稿持久化（切换/刷新后恢复未发送内容，taskStore.drafts）。
 *
 * 架构（借鉴 deepseek-harness「machine → decorations → facade/hub → UI 只渲染」
 * 分层，见 ``docs/输入组件优化.md``）：
 * - ``useInputState``：可发送/可停止派生 + 同步提交互斥锁（本组件只读快照）。
 * - ``useSendInput``：发送事务副作用（guard/创建任务或轮次/trace/perf/日志/失败回滚）。
 * - ``taskStore.drafts``：跨 task 草稿持久化。
 *
 * 不在职责范围内（已抽离到 `components/chat/TaskHeaderBar.tsx`）：
 * - Agent 选择器（AgentSelector）。
 * - 模型选择器（ModelSelector）。
 * - 模型厂商配置中心（ProviderSettingsDialog）。
 *
 * 按钮置灰语义（方案 §阶段 1.5）：未显式选择模型（selectedModelName=null）时
 * 发送按钮直接 disabled，并 hover 显示 tooltip「请先选择模型」；guardSend 作为
 * 防御兜底仍保留——模型被删（model_missing）/ 厂商 Key 未配置（api_key_missing）
 * 等运行时场景仍由 guardSend 拦截并内联展示原因。
 *
 * @module components/layout/InputBar
 */

import { useEffect, useRef, useState } from "react";
import { Mic, Plus, Send, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { ContextUsageRing } from "@/components/chat/ContextUsageRing";
import { useTask } from "@/hooks/useTask";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useModelSendGuard } from "@/hooks/useModelSendGuard";
import { useInputState } from "./useInputState";
import { useSendInput } from "./useSendInput";

/**
 * InputBar 底部输入区组件。
 *
 * @param props - 组件属性。
 * @param props.onOpenSettings - 可选回调：发送拦截规则要求打开模型厂商配置中心时调用，
 *   由宿主联动打开 ChatPanel 顶部的 ProviderSettingsDialog。
 */
export function InputBar({ onOpenSettings }: { onOpenSettings?: () => void }) {
  const { createTask, createTurn, cancelTurn, operation } = useTask();
  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  // 读取当前 active task 维度下的 streaming turn（按 task 隔离，支持多 task 并发流式）。
  const streamingTurnId = useTurnStore((s) =>
    s.streamingTurnIds[activeTaskId ?? ""] ?? null,
  );
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const selectedModelName = useTaskStore((s) => s.selectedModelName);
  const setInputDraft = useTaskStore((s) => s.setInputDraft);

  // 本地输入值：仅作受控渲染与 IME/键盘交互；持久化事实源是 taskStore.drafts。
  // 初始值取当前任务草稿（刷新/切换后恢复未发送内容）。
  const [inputValue, setInputValue] = useState(() =>
    activeTaskId ? useTaskStore.getState().drafts[activeTaskId] ?? "" : "",
  );
  const [guardMessage, setGuardMessage] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // IME 组合态标记：组合期间 Enter 不触发发送（含 keyCode 229 兜底，借鉴外部三路判定）。
  const composingRef = useRef(false);
  // 上一次活跃任务 ID：用于「切换任务才回灌草稿」，避免输入过程被 store 回写打断。
  const prevTaskIdRef = useRef<string | null>(activeTaskId);
  const { guardSend } = useModelSendGuard();

  const trimmedInput = inputValue.trim();
  const hasModelSelected = Boolean(selectedModelName);
  const noModelSelected = !hasModelSelected;

  // 可用性派生 + 提交互斥锁（外置，组件只读快照）。
  const inputState = useInputState({
    hasDraftText: Boolean(trimmedInput),
    isLoading: operation.loading,
    isStreaming: Boolean(streamingTurnId),
    hasContext: Boolean(activeTaskId || activeWorkspaceId),
    hasModelSelected,
  });
  const { canSend, canStop, isSubmitting } = inputState;

  const { send, stop } = useSendInput(
    {
      activeTaskId,
      activeWorkspaceId,
      guardSend,
      createTurn,
      createTask,
      cancelTurn,
      streamingTurnId,
      setInputDraft,
      setGuardMessage,
      onOpenSettings,
    },
    inputState,
  );

  // 草稿回灌：仅在「切换任务」时把目标任务草稿写回本地输入框（借鉴外部 mount 回灌），
  // 输入过程不订阅 draft，避免每次敲击都触发回写与重聚焦导致光标跳动。
  useEffect(() => {
    if (prevTaskIdRef.current === activeTaskId) {
      return;
    }
    prevTaskIdRef.current = activeTaskId;
    const next = activeTaskId ? useTaskStore.getState().drafts[activeTaskId] ?? "" : "";
    setInputValue(next);
    // 切换任务后自动聚焦（preventScroll 避免滚动跳动，借鉴外部 focus 管理）。
    inputRef.current?.focus({ preventScroll: true });
  }, [activeTaskId]);

  // 用户切换模型后清除拦截提示（2026-08-18 无 Auto 语义）。
  useEffect(() => {
    setGuardMessage(null);
  }, [selectedModelName]);

  const placeholder = !activeWorkspaceId
    ? "请先选择工作区再开始对话..."
    : activeTaskId
      ? "给 Agent 下达任务..."
      : "输入任务内容开始对话...";

  const handleSend = () => {
    void send(trimmedInput, {
      // 乐观提交：清空本地输入框（发送在飞即清，失败再经 restore 回滚）。
      commit: () => setInputValue(""),
      // 失败回滚：把草稿写回本地输入框（失败不丢草稿）。
      restore: (restored: string) => setInputValue(restored),
    });
  };

  const handleStop = () => {
    void stop();
  };

  /** 输入变更：本地渲染 + 同步 store 草稿持久化（单一出口 setInputDraft）。 */
  const handleChange = (value: string) => {
    setInputValue(value);
    if (activeTaskId) {
      setInputDraft(activeTaskId, value);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    // 长按 Enter 防连发（机关枪防护，借鉴外部 InputBar.tsx:323）。
    if (e.repeat) {
      return;
    }
    // IME 三路判定：composition 事件标记 || 原生 isComposing || keyCode 229 兜底，
    // 避免中文输入法组合态下误发送（借鉴外部 InputBar.tsx:284-286）。
    const composing = composingRef.current || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229;
    if (e.key === "Enter" && !e.shiftKey && !composing) {
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
              <Button
                variant="ghost"
                size="icon"
                className="h-9 w-9 shrink-0 text-muted-foreground"
                // 点击工具按钮不抢输入框焦点（keepFocus，借鉴外部 onMouseDown preventDefault）。
                onMouseDown={(e) => e.preventDefault()}
              >
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
            ref={inputRef}
            value={inputValue}
            onChange={(e) => handleChange(e.target.value)}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={() => {
              // 延迟清位：防御 Safari 等浏览器 compositionend 与 keydown 时序问题
              // （借鉴外部 InputBar.tsx:109-119 的 setTimeout 兜底）。
              setTimeout(() => {
                composingRef.current = false;
              }, 10);
            }}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            aria-label="任务输入框"
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
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-muted-foreground"
                      onMouseDown={(e) => e.preventDefault()}
                    >
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
                    disabled={!canSend || isSubmitting}
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

      {/* 发送前模型校验拦截提示（如「请先选择模型」），role=status 供读屏播报 */}
      {guardMessage && (
        <div className="mx-auto mt-1.5 max-w-content">
          <p role="status" aria-live="polite" className="text-sm text-destructive">
            {guardMessage}
          </p>
        </div>
      )}
    </div>
  );
}

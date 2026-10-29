"use client";

import { ArrowDownIcon, ArrowUpIcon, CheckIcon, CopyIcon, GitForkIcon, ListEndIcon, Loader2Icon, PencilIcon, PlayIcon, XIcon, ZapIcon } from "lucide-react";
import { useContext, createContext, memo, Profiler, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type FC, type ReactNode } from "react";
import {
  AuiIf,
  ActionBarPrimitive,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  unstable_useComposerInput,
  useAui,
  useAuiState,
  type AssistantState,
} from "@assistant-ui/react";

import { StopButton } from "@/components/assistant/stop-button";
import { RunDiagnosticsActions } from "@/components/assistant/run-diagnostics-actions";
import { RunUsageDisplay, TaskContextUsage } from "@/components/assistant/usage-display";
import { ComposerControls } from "@/components/composer/composer-controls";
import { ComposerFeedback, ComposerFeedbackProvider } from "@/components/composer/composer-feedback";
import { ComposerPromptToolbar } from "@/components/composer/composer-prompt-toolbar";
import { UserMessageText } from "@/components/workspace-references/user-message-text";
import { MarkdownText } from "@/components/markdown-text";
import { PartRenderBoundary } from "@/components/assistant-ui/elements/part-render-boundary";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import {
  Reasoning,
  ReasoningContent,
  ReasoningRoot,
  ReasoningText,
  ReasoningTrigger,
} from "@/components/assistant-ui/elements/reasoning.aui";
import {
  ToolGroupContent,
  ToolGroupRoot,
  ToolGroupTrigger,
} from "@/components/assistant-ui/elements/tool-group.aui";
import { summarizeToolGroup } from "@/components/assistant-ui/elements/tool-group-status";
import { ASSISTANT_PART_STACK_CLASS } from "@/components/assistant-ui/elements/tool-layout-tokens";
import { ToolPart } from "@/components/assistant-ui/tools/tool-part";
import { readToolArtifact } from "@/components/assistant-ui/tools/types";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import {
  deriveComposerAction,
  editBlockedReason,
  isEditableLatestUserMessage,
  isQueuedUserMessage,
  canResumeStoppedRun,
} from "@/lib/assistant/conversation-actions";
import { readRuntimeTransportState } from "@/lib/assistant/transport-state-operations";
import { editIntentRunConfig, toEditableUserMessageDraft, type EditIntent } from "@/lib/assistant/converter";
import { localFileTokenIds } from "@/lib/assistant/attachments/local-file-token";
import { editableDocumentAttachments } from "@/lib/assistant/editable-user-document";
import {
  applyEditDraftToComposer,
  beginEditComposerOperation,
  consumePendingEditComposerStart,
  currentEditComposerOperation,
  discardPendingEditComposerDraft,
  editableDraftAttachments,
  endEditComposerOperation,
  hasPendingEditComposerStart,
  isCurrentEditComposerOperation,
  isEditComposerOperationActive,
  subscribeEditComposerOperations,
  takePendingEditComposerDraft,
  type EditDraftAttachment,
} from "@/lib/assistant/edit-composer-draft";
import type { InputDelivery, TransportActivity, TransportOutcome, TransportToolStatus } from "@/lib/assistant/contract";
import { readStoredInputDelivery, writeStoredInputDelivery } from "@/lib/input-delivery-storage";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";
import type { ToolGroupCatalog } from "@/lib/api/tools";
import type { TransportRunLaunch } from "@/lib/assistant/contract";
import { RUN_LAUNCH_LOCKED_HINT, useActiveRunLaunch } from "@/lib/assistant/run-launch";
import { AttachmentWorkspaceContext } from "@/components/assistant-ui/elements/attachment-context";
import { VirtualizedThreadMessages } from "@/components/assistant-ui/elements/virtualized-thread-messages";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { UserMessageAttachments } from "@/components/assistant-ui/elements/user-message-attachments";
import {
  ComposerAttachmentButton,
  ComposerSendButton,
  ComposerAttachments,
  InlineComposerInput,
} from "@/components/assistant-ui/elements/attachment.aui";
import { COMPOSER_EDITOR_CLASS_NAME, COMPOSER_PLACEHOLDER } from "@/components/composer/composer-editor";
import { COMPOSER_SURFACE_CLASS_NAME } from "@/components/composer/composer-surface";
import { ComposerInsertionProvider } from "@/components/composer/composer-insertion-context";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { notify } from "@/lib/notify";

export type ThreadProps = {
  /** 只渲染 canonical message UI，不提供写入操作。 */
  readonly?: boolean;
  taskId: number;
  workspaceId?: number | null;
  toolGroups?: ToolGroupCatalog[];
  selectedToolGroups?: string[];
  onSelectedToolGroupsChange?: (groups: string[]) => void;
  toolGroupsLoading?: boolean;
  toolGroupsError?: string | null;
  workspaceRoot?: string;
  /** 正在 fork 的消息 id；fork 按消息切分。 */
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
  /** 可写对话必传（由 AssistantThreadSurface 提供）；只读对话不渲染 Composer，不需要。 */
  onResumeBusiness?: () => Promise<void>;
  onCancelRequested?: () => void;
  onCancelResult?: (accepted: boolean) => void;
  /** 用户已请求停止 Task 的当前活动、尚未收敛（Task 级，不按 runId）。 */
  cancelling?: boolean;
  performanceProbe?: AssistantPerformanceProbe | null;
};

const RESUME_FEEDBACK_TIMEOUT_MS = 15_000;
type ThreadContextValue = Pick<ThreadProps, "forkingMessageId" | "onForkMessage" | "onResumeBusiness" | "onCancelRequested" | "onCancelResult" | "workspaceRoot" | "cancelling" | "readonly" | "toolGroups" | "selectedToolGroups" | "onSelectedToolGroupsChange" | "toolGroupsLoading" | "toolGroupsError" | "taskId">;
const ThreadContext = createContext<ThreadContextValue | null>(null);

/** 线程内部组件只在 Thread 下渲染；缺 Provider 是装配错误，直接暴露。 */
function useThreadContext(): ThreadContextValue {
  const context = useContext(ThreadContext);
  if (!context) throw new Error("ThreadContext 只能在 Thread 内使用");
  return context;
}

type AssistantGroupKey = "group-reasoning" | "group-tool-trace";

type ToolTraceGroupProps = {
  indices: readonly number[];
  children: ReactNode;
};

/**
 * 使用各工具的后端生命周期状态渲染聚合后的工具轨迹。
 *
 * 没有工具结果时，assistant-ui 会从 assistant message status 推导工具状态；
 * assistant 继续后续步骤时，已完成的工具可能因此仍显示运行中，所以这里直接读取
 * Transport artifact 计算聚合状态。只有一个调用时不折叠成组：组标题只会复述那一行，
 * 还要多点一次才能看到它。
 */
const ToolTraceGroup: FC<ToolTraceGroupProps> = ({ indices, children }) => {
  if (indices.length === 1) return <>{children}</>;
  return <CollapsedToolTraceGroup indices={indices}>{children}</CollapsedToolTraceGroup>;
};

const CollapsedToolTraceGroup: FC<ToolTraceGroupProps> = ({ indices, children }) => {
  const statusKey = useAuiState((state) => indices.map((index) => {
    const part = state.message.parts[index];
    if (part?.type !== "tool-call") return "unknown";
    return readToolArtifact(part.artifact).backendStatus;
  }).join("|"));
  // 组默认折叠，运行中在标题上露出最新一个未结束调用的动词，让用户知道 Agent 正在做什么。
  const activeTitle = useAuiState((state) => {
    for (let i = indices.length - 1; i >= 0; i -= 1) {
      const part = state.message.parts[indices[i]];
      if (part?.type !== "tool-call") continue;
      const artifact = readToolArtifact(part.artifact);
      if (artifact.backendStatus === "pending" || artifact.backendStatus === "running") {
        return artifact.presentation.verb ?? part.toolName;
      }
    }
    return null;
  });
  const summary = useMemo(
    () => summarizeToolGroup(
      (statusKey ? statusKey.split("|") : []) as TransportToolStatus[],
    ),
    [statusKey],
  );

  return (
    <ToolGroupRoot>
      <ToolGroupTrigger summary={summary} activeTitle={activeTitle} />
      <ToolGroupContent>{children}</ToolGroupContent>
    </ToolGroupRoot>
  );
};

const assistantMessageGroupBy = (
  part: { type: string; artifact?: unknown },
): readonly AssistantGroupKey[] => {
  if (part.type === "reasoning") return ["group-reasoning"];
  if (part.type !== "tool-call") return [];
  const surface = readToolArtifact(part.artifact).presentation.surface;
  return surface === "standalone" ? [] : ["group-tool-trace"];
};

const isNewChatView = (state: AssistantState) => state.thread.messages.length === 0;

export const Thread: FC<ThreadProps> = memo(function Thread({ readonly = false, taskId, workspaceId, toolGroups, selectedToolGroups, onSelectedToolGroupsChange, toolGroupsLoading, toolGroupsError, workspaceRoot, forkingMessageId = null, onForkMessage, onResumeBusiness, onCancelRequested, onCancelResult, cancelling = false, performanceProbe = null }) {
  const isEmpty = useAuiState(isNewChatView);
  const viewportRef = useRef<HTMLDivElement>(null);
  const messageComponents = useMemo(() => ({ Message: ThreadMessage }), []);
  return (
    <ThreadContext.Provider value={{ taskId, toolGroups, selectedToolGroups, onSelectedToolGroupsChange, toolGroupsLoading, toolGroupsError, workspaceRoot, forkingMessageId, onForkMessage, onResumeBusiness, onCancelRequested, onCancelResult, cancelling, readonly }}>
      <AttachmentWorkspaceContext.Provider value={workspaceId ?? undefined}>
      <ThreadPrimitive.Root className="aui-root aui-thread-root bg-background flex h-full min-h-0 min-w-0 flex-col">
        {/*
          顶部锚点固定本轮用户消息，答案在其下方增长，流式 token 不会持续把滚动位置重新
          钉回底部。这里显式关闭 autoScroll，因为该模式默认值为 false；中途追踪底部会
          换取稳定视口。消息根节点另外通过 content-visibility 工具跳过屏幕外布局和绘制，
          当前尚未校准 intrinsic size。
          视口不能加 CSS scroll-smooth：虚拟列表重新测量行高时会程序化修正 scrollTop，
          平滑滚动会把这些修正变成可见的漂移；只有用户点击「回到底部」才使用平滑滚动。
        */}
        <ThreadPrimitive.Viewport ref={viewportRef} turnAnchor="top" autoScroll={false} className="relative flex min-h-0 min-w-0 w-full flex-1 flex-col overflow-x-hidden overflow-y-auto">
            <div aria-hidden="true" className="scroll-fade-top" />
            <div className={cn("mx-auto flex min-w-0 w-full max-w-3xl flex-1 flex-col px-4 pt-6", isEmpty && "justify-center")}>
              <VirtualizedThreadMessages
                scrollElementRef={viewportRef}
                components={messageComponents}
                performanceProbe={performanceProbe}
              />
              {!readonly && (
                <ThreadPrimitive.ViewportFooter className="bg-background sticky bottom-0 mt-auto flex min-w-0 flex-col gap-4 pb-4 md:pb-6">
                  <div aria-hidden="true" className="scroll-fade-bottom" />
                  <ScrollToBottomButton />
                  <ComposerBoundary
                    taskId={taskId}
                    workspaceRoot={workspaceRoot}
                    performanceProbe={performanceProbe}
                  />
                </ThreadPrimitive.ViewportFooter>
              )}
            </div>
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
      </AttachmentWorkspaceContext.Provider>
    </ThreadContext.Provider>
  );
});

/**
 * 「回到底部」按钮：仅在视口未贴底时可见（primitive 在贴底时禁用，样式上淡出）。
 * 刻意保持克制：不加运行中提示点，流式状态已由 composer 的停止按钮表达。
 * 轮廓用低透明度 ring 而非实色边框：浅色下靠柔和阴影浮起，深色下阴影不可见，靠 ring 保留轮廓。
 * 必须显式覆盖 TooltipIconButton 默认的 size-6 p-1，否则内边距会挤掉图标空间。
 */
const ScrollToBottomButton: FC = () => (
  <ThreadPrimitive.ScrollToBottom
    behavior="smooth"
    render={
      <TooltipIconButton
        tooltip="回到底部"
        variant="ghost"
        className="text-muted-foreground hover:text-foreground bg-background hover:bg-muted/60 ring-foreground/[0.06] absolute -top-11 size-8 self-center rounded-full p-0 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_4px_12px_rgb(0_0_0/0.08)] ring-1 transition-[opacity,translate] duration-150 disabled:translate-y-1 disabled:opacity-0 dark:ring-white/10 [&_svg]:size-4"
      />
    }
  >
    <ArrowDownIcon />
  </ThreadPrimitive.ScrollToBottom>
);

const ComposerBoundary = memo(function ComposerBoundary({
  taskId,
  workspaceRoot,
  performanceProbe,
}: {
  taskId: number;
  workspaceRoot?: string;
  performanceProbe?: AssistantPerformanceProbe | null;
}) {
  const composer = <Composer taskId={taskId} workspaceRoot={workspaceRoot} />;
  if (!performanceProbe) return composer;
  return (
    <Profiler
      id="assistant-composer"
      onRender={(_, phase, actualDuration) => performanceProbe.recordReactCommit({
        scope: "composer",
        id: "assistant-composer",
        phase,
        actualDurationMs: actualDuration,
      })}
    >
      {composer}
    </Profiler>
  );
});

const Composer = memo(function Composer({ taskId, workspaceRoot }: { taskId: number; workspaceRoot?: string }) {
  // 运行期配置锁定：活动 Run 期间模型与工具显示 Run 冻结的配置且只读，发送也用这份配置。
  const runLaunch = useActiveRunLaunch();
  // 运行中照常可以发送（按投递方式并入或排到下一轮）；只有停止请求在途时不提交，避免与停止交错。
  const { cancelling = false } = useThreadContext();
  return (
    <ComposerInsertionProvider>
      <ComposerFeedbackProvider>
        <ComposerPrimitive.Root className={COMPOSER_SURFACE_CLASS_NAME}>
          <div className="min-h-0 min-w-0 w-full">
            <ComposerAttachments />
            <InlineComposerInput
              persistTaskDraft
              placeholder={COMPOSER_PLACEHOLDER}
              className={COMPOSER_EDITOR_CLASS_NAME}
              autoFocus
              submitDisabled={cancelling}
              aria-label="消息输入"
            />
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 px-1">
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
              <ThreadComposerPromptToolbar lock={runLaunch === null ? null : { kind: "run", launch: runLaunch }} />
              <TaskContextUsage />
              <div className="min-w-0 flex-1">
                <ComposerControls
                  scope={{ kind: "task", id: taskId }}
                  runtimeModelContext
                  locked={runLaunch}
                />
              </div>
            </div>
            <ComposerAttachmentButton workspaceRoot={workspaceRoot} />
            <ComposerAction taskId={taskId} />
          </div>
          <ComposerFeedback />
        </ComposerPrimitive.Root>
      </ComposerFeedbackProvider>
    </ComposerInsertionProvider>
  );
});

/**
 * 工具组配置的锁定来源：`run` 为活动 Run 冻结的配置（按其冻结的工具组名回显、只读）；`queued` 为排队
 * 消息编辑，配置沿用发送时的值且快照不投影它，因此不显示工具组开关。
 */
const EMPTY_TOOL_GROUPS: ToolGroupCatalog[] = [];

type ToolGroupsLock = { kind: "run"; launch: TransportRunLaunch } | { kind: "queued" } | null;

const ThreadComposerPromptToolbar: FC<{ disabled?: boolean; lock?: ToolGroupsLock }> = ({ disabled = false, lock = null }) => {
  const composer = unstable_useComposerInput();
  const context = useThreadContext();
  const toolGroups = context.toolGroups ?? EMPTY_TOOL_GROUPS;
  const lockedGroups = lock?.kind === "run" ? lock.launch.disabledToolGroups : null;
  return (
    <ComposerPromptToolbar
      toolGroups={toolGroups}
      selectedToolGroups={lockedGroups ?? context.selectedToolGroups ?? []}
      onSelectedToolGroupsChange={lock === null ? context.onSelectedToolGroupsChange : undefined}
      toolGroupsLoading={context.toolGroupsLoading}
      toolGroupsError={context.toolGroupsError}
      disabled={disabled || composer.isDisabled}
      toolGroupsLockedHint={lockedGroups === null ? undefined : RUN_LAUNCH_LOCKED_HINT}
      showToolGroups={lock?.kind !== "queued"}
    />
  );
};

const ComposerAction: FC<{ taskId: number }> = ({ taskId }) => {
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const isDraftEmpty = useAuiState((state) =>
    state.composer.text.trim().length === 0 && state.composer.attachments.length === 0,
  );
  const { cancelling = false, onResumeBusiness, onCancelRequested, onCancelResult } = useThreadContext();
  const canResume = useAuiState((state) => canResumeStoppedRun(readRuntimeTransportState(state.thread.state)));
  const isCancelling = cancelling;
  const action = deriveComposerAction({ isRunning, isDraftEmpty, canResume, isCancelling });
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  // 投递方式只在运行中有意义；偏好存于 storage，提交时由 Transport 宿主读取同一份值。
  const [delivery, setDelivery] = useState<InputDelivery>(readStoredInputDelivery);
  // toast「重试」持有旧闭包：用 ref 读取当下是否仍可继续、是否已有请求在途，避免重复 business resume。
  const resumingRef = useRef(false);
  const actionRef = useRef(action);
  actionRef.current = action;

  useEffect(() => {
    void frontendLog("DEBUG", "composer_action_derived", "Composer action 状态发生变化", {
      data: { taskId, action, isRunning, isDraftEmpty, canResume, isCancelling },
    });
  }, [action, canResume, isCancelling, isDraftEmpty, isRunning, taskId]);

  useEffect(() => {
    if (isRunning || !canResume) {
      resumingRef.current = false;
      setResuming(false);
      setResumeError(null);
    }
  }, [canResume, isRunning]);

  useEffect(() => {
    if (!resuming) return;
    const timeout = window.setTimeout(() => {
      const message = "继续运行请求未得到响应，请重试";
      resumingRef.current = false;
      setResuming(false);
      setResumeError(message);
      notify.error("继续运行失败", { error: new UserFacingError(message) });
      void frontendLog("WARNING", "composer_resume_feedback_timeout", "继续运行请求超时未建立连接", {
        data: { taskId, timeoutMs: RESUME_FEEDBACK_TIMEOUT_MS },
      });
    }, RESUME_FEEDBACK_TIMEOUT_MS);
    return () => window.clearTimeout(timeout);
  }, [resuming, taskId]);

  if (action === "stop") {
    return <ComposerPrimitive.Cancel render={<StopButton taskId={taskId} onCancelRequested={onCancelRequested} onCancelResult={onCancelResult} />} />;
  }

  if (action === "cancelling") {
    return <StopButton taskId={taskId} isCancelling />;
  }

  if (action === "resume") {
    const resume = async () => {
      if (resumingRef.current || actionRef.current !== "resume") return;
      resumingRef.current = true;
      setResuming(true);
      setResumeError(null);
      try {
        await onResumeBusiness!();
      } catch (error) {
        resumingRef.current = false;
        setResuming(false);
        const fallback = "继续运行失败，请检查本机后端状态";
        setResumeError(userErrorMessage(error, fallback));
        notify.error("继续运行失败", { error, fallback, action: { label: "重试", onClick: () => void resume() } });
        void frontendLog("ERROR", "composer_resume_failed", "继续运行请求失败", {
          data: { taskId },
          error,
        });
      }
    };

    return (
      <Button
        type="button"
        size="icon"
        className="size-8 rounded-full"
        aria-label={resuming ? "正在继续运行" : "继续运行"}
        title={resumeError ?? "继续运行"}
        disabled={resuming}
        onClick={() => void resume()}
      >
        {resuming ? <Loader2Icon className="size-4 animate-spin" /> : <PlayIcon className="size-4 fill-current" />}
      </Button>
    );
  }

  return (
    <div className="flex items-center gap-2">
      {isRunning && (
        <DeliveryToggle
          delivery={delivery}
          onChange={(next) => {
            writeStoredInputDelivery(next);
            setDelivery(next);
          }}
        />
      )}
      {isRunning && <ComposerPrimitive.Cancel render={<StopButton taskId={taskId} onCancelRequested={onCancelRequested} onCancelResult={onCancelResult} />} />}
      <ComposerSendButton
        aria-label={isRunning ? DELIVERY_LABELS[delivery].send : "发送"}
        title={isRunning ? DELIVERY_LABELS[delivery].title : undefined}
      >
        <ArrowUpIcon className="size-4" />
      </ComposerSendButton>
    </div>
  );
};

const DELIVERY_LABELS: Record<InputDelivery, { toggle: string; send: string; title: string; queued: string }> = {
  immediate: {
    toggle: "立即",
    send: "立即发送",
    title: "立即发送：在下一步并入当前运行；等待审批时视为驳回并附上这条消息",
    queued: "下一步并入",
  },
  next_run: {
    toggle: "排队",
    send: "加入队列",
    title: "排队发送：交给下一轮，当前运行正常结束后开始；运行中更换模型的消息总是这样发送",
    queued: "下一轮处理",
  },
};

/** 运行中发送消息时切换「立即并入 / 本轮结束后」；无活动 Run 时后端直接新开 Run，不显示。 */
const DeliveryToggle: FC<{ delivery: InputDelivery; onChange: (delivery: InputDelivery) => void }> = ({ delivery, onChange }) => {
  const labels = DELIVERY_LABELS[delivery];
  const Icon = delivery === "immediate" ? ZapIcon : ListEndIcon;
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-8 gap-1 rounded-full px-2 text-xs text-muted-foreground"
      aria-label={`发送方式：${labels.toggle}`}
      title={labels.title}
      onClick={() => onChange(delivery === "immediate" ? "next_run" : "immediate")}
    >
      <Icon className="size-3.5" />
      {labels.toggle}
    </Button>
  );
};

const ThreadMessage: FC = () => {
  const role = useAuiState((state) => state.message.role);
  return role === "user" ? <UserMessage /> : <AssistantMessage />;
};

const UserMessage: FC = () => {
  const aui = useAui();
  const messageId = useAuiState((state) => state.message.id);
  const isEditing = useAuiState((state) => state.composer.isEditing);
  const pendingEditStart = useSyncExternalStore(
    subscribeEditComposerOperations,
    () => hasPendingEditComposerStart(messageId),
    () => false,
  );
  const readonly = useThreadContext().readonly === true;
  useEffect(() => {
    if (!pendingEditStart || isEditing || !consumePendingEditComposerStart(messageId)) return;
    try {
      aui.composer.beginEdit();
    } catch (error) {
      discardPendingEditComposerDraft(messageId);
      const generation = currentEditComposerOperation(messageId);
      if (generation !== undefined) endEditComposerOperation(messageId, generation);
      void frontendLog("WARNING", "assistant_edit_composer_restore_rejected", "消息行未能重新进入编辑态", {
        data: { messageId },
        error,
      });
    }
  }, [aui, isEditing, messageId, pendingEditStart]);
  if (isEditing && !readonly) return <UserEditMessage />;
  return <UserMessageView />;
};

const UserMessageView: FC = () => {
  const messageId = useAuiState((state) => state.message.id);
  const hasText = useAuiState((state) => state.message.parts.some(
    (part) => part.type === "text" && part.text.trim().length > 0,
  ));
  const queuedDelivery = useAuiState((state) => readRuntimeTransportState(state.thread.state)?.queued_messages?.find((message) => message.id === messageId)?.delivery ?? null);
  const readonly = useThreadContext().readonly === true;
  const canEdit = useAuiState((state) => {
    if (readonly) return false;
    const transportState = readRuntimeTransportState(state.thread.state);
    // 排队中的消息原地编辑，不影响正在运行的 Run。
    if (isQueuedUserMessage(transportState, messageId)) return true;
    if (!isEditableLatestUserMessage(transportState, messageId)) return false;

    // Transport runtime 可能在 canonical snapshot 到达前乐观渲染新的用户命令；
    // 此期间不能因为旧 snapshot 仍在，就把上一条用户消息重新标记为可编辑。
    const latestRenderedUserMessageId = [...state.thread.messages]
      .reverse()
      .find((message) => message.role === "user")?.id;
    return latestRenderedUserMessageId === messageId;
  });

  return (
    <MessagePrimitive.Root
      data-role="user"
      className="group flex flex-col items-end px-2 [content-visibility:auto] [contain-intrinsic-size:auto_6rem]"
    >
      <div className="flex max-w-[85%] flex-col items-end gap-2">
        {queuedDelivery && <span className="text-xs text-muted-foreground">{DELIVERY_LABELS[queuedDelivery].queued}</span>}
        <UserMessageAttachments />
        {hasText && (
          <div className="bg-primary/10 text-foreground rounded-2xl px-4 py-2.5 text-sm leading-relaxed wrap-break-word">
            <MessagePrimitive.Parts>{({ part }) => part.type === "text" ? (
              <PartRenderBoundary
                renderer="用户 Markdown"
                fallback={({ onRetry }) => (
                  <RenderErrorCard
                    scope="markdown"
                    label="用户内容"
                    fallbackText={part.text}
                    onRetry={onRetry}
                  />
                )}
              >
                <UserMessageText text={part.text} status={part.status} />
              </PartRenderBoundary>
            ) : null}</MessagePrimitive.Parts>
          </div>
        )}
      </div>
      <div className="mt-1 h-6 shrink-0">
        <ActionBarPrimitive.Root
          className="flex h-6 gap-1 opacity-0 transition-opacity pointer-events-none group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100"
        >
          {canEdit && (
            <ActionBarPrimitive.Edit render={<TooltipIconButton tooltip={queuedDelivery ? "编辑" : "编辑并重跑"} aria-label={queuedDelivery ? "编辑" : "编辑并重跑"} size="sm" />}>
              <PencilIcon />
            </ActionBarPrimitive.Edit>
          )}
          <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip="复制" aria-label="复制" size="sm" />}>
            <AuiIf condition={(state) => state.message.isCopied}><CheckIcon /></AuiIf>
            <AuiIf condition={(state) => !state.message.isCopied}><CopyIcon /></AuiIf>
          </ActionBarPrimitive.Copy>
        </ActionBarPrimitive.Root>
      </div>
    </MessagePrimitive.Root>
  );
};

/** 写入 effect 需要的一次性草稿快照；与 message 对象解耦，避免写入被 transport 更新打断。 */
type EditDraftSnapshot = {
  text: string;
  attachments: readonly EditDraftAttachment[];
  source: "recovery_override" | "canonical_message";
  messageAttachmentCount: number;
  messageContentTypes: readonly string[];
};

const UserEditMessage: FC = () => {
  const aui = useAui();
  const message = useAuiState((state) => state.message);
  const { taskId, workspaceRoot } = useThreadContext();
  const transportState = useAuiState((state) => readRuntimeTransportState(state.thread.state));
  const [restoreOverride] = useState(() => takePendingEditComposerDraft(message.id));
  // 编辑意图在挂载时确定一次（被拒后恢复的编辑框沿用被拒命令的意图），经 runConfig 随命令提交；
  // 排队消息原地编辑只改正文与附件、沿用发送时的配置，已并入的编辑会开新 Run、可改配置。
  const [intent, setIntent] = useState<EditIntent>(
    () => restoreOverride?.intent ?? (isQueuedUserMessage(transportState, message.id) ? "queued" : "rerun"),
  );
  const queuedEdit = intent === "queued";
  // 编辑期间前提失效（排队消息已被并入、重跑目标已不是可重跑的最新一条）时给出原因并禁止提交。
  const blockedReason = editBlockedReason(transportState, message.id, intent);
  // 排队消息已被并入时，只在它此刻确实能编辑重跑时提供改道入口，由用户明确选择。
  const canSwitchToRerun = queuedEdit && blockedReason !== null
    && editBlockedReason(transportState, message.id, "rerun") === null;
  useLayoutEffect(() => {
    aui.composer.setRunConfig(editIntentRunConfig(intent));
  }, [aui.composer, intent]);
  const canonicalEditDraft = useMemo(
    () => message.role === "user" ? toEditableUserMessageDraft(message) : null,
    [message],
  );
  const editDraft = restoreOverride ?? canonicalEditDraft;
  const editText = editDraft?.text ?? "";
  const editAttachments = canonicalEditDraft && !restoreOverride
    ? editableDocumentAttachments(canonicalEditDraft.document)
    : editDraft?.attachments ?? [];
  const hydratedMessageId = useRef<string | null>(null);
  // 草稿快照：写入内容与 message 对象解耦——写入 effect 只依赖消息身份，后续任何一次
  // transport 更新引起的重渲染都不会再打断正在进行的草稿写入。
  const draftRef = useRef<EditDraftSnapshot | null>(null);
  // 图片写入进度，仅供日志复盘；普通文件只登记元数据，不计入 AUI 附件数。
  const hydrationProgressRef = useRef<{ expected: number; added: number; aborted: string | null }>({
    expected: 0,
    added: 0,
    aborted: null,
  });
  const [isHydrating, setIsHydrating] = useState(true);
  const isRecoveryActive = useSyncExternalStore(
    subscribeEditComposerOperations,
    () => isEditComposerOperationActive(message.id),
    () => false,
  );
  // 回车与保存按钮共用这一个开关：任何「此刻不能提交」的条件只在这里加一次。
  const sendBlocked = isHydrating || isRecoveryActive || blockedReason !== null;

  // 先于写入 effect 执行：每次提交同步一次草稿快照（无稳定身份的附件在此被剔除）。
  useLayoutEffect(() => {
    draftRef.current = editDraft
      ? {
          text: editText,
          attachments: editableDraftAttachments(editAttachments),
          source: restoreOverride ? "recovery_override" : "canonical_message",
          messageAttachmentCount: message.attachments?.length ?? 0,
          messageContentTypes: message.content.map((part) => part.type),
        }
      : null;
  });

  useLayoutEffect(() => {
    const draft = draftRef.current;
    if (!draft || hydratedMessageId.current === message.id) return;
    hydratedMessageId.current = message.id;
    const generation = currentEditComposerOperation(message.id) ?? beginEditComposerOperation(message.id);
    const composer = aui.composer;
    const expectedAttachments = draft.attachments.length;
    const textTokenIds = localFileTokenIds(draft.text);
    hydrationProgressRef.current = { expected: draft.attachments.filter((attachment) => attachment.type === "image").length, added: 0, aborted: null };
    void frontendLog("INFO", "edit_composer_hydration_started", "编辑草稿开始写入输入框", {
      data: {
        messageId: message.id,
        source: draft.source,
        textLength: draft.text.length,
        textTokenIds,
        attachmentIds: draft.attachments.map((attachment) => attachment.id),
        attachmentCount: expectedAttachments,
      },
    });
    // 有 token 却投影不出附件对象时，输入框只能渲染占位态。先把这种组合显式记录下来，
    // 后续排查不必再从界面现象反推。
    if (expectedAttachments === 0 && textTokenIds.length > 0) {
      void frontendLog("WARNING", "edit_composer_draft_without_attachments", "编辑草稿含附件 token 但未投影出附件对象", {
        data: {
          messageId: message.id,
          textTokenIds,
          source: draft.source,
          messageAttachmentCount: draft.messageAttachmentCount,
          messageContentTypes: draft.messageContentTypes,
        },
      });
    }

    // 共用恢复器登记普通文件元数据、恢复图片并清理 AUI 提取的普通文件副本。
    void (async () => {
      if (draft.source === "recovery_override") {
        // 编辑 composer 初次挂载时会先从 canonical message 初始化；等该同步提交后再覆盖失败请求草稿。
        await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      }
      const result = await applyEditDraftToComposer({
        composer,
        text: draft.text,
        attachments: draft.attachments,
        isStillCurrent: () => isCurrentEditComposerOperation(message.id, generation),
      });
      hydrationProgressRef.current.added = result.addedCount;
      if (result.superseded) {
        hydrationProgressRef.current.aborted = "superseded_generation";
        void frontendLog("WARNING", "edit_composer_hydration_aborted", "编辑草稿写入被更新的编辑代际取代，文本未改写", {
          data: {
            messageId: message.id,
            reason: "superseded_generation",
            addedCount: result.addedCount,
            expectedCount: result.expectedCount,
          },
        });
      } else if (result.error) {
        hydrationProgressRef.current.aborted = "write_failed";
        void frontendLog("ERROR", "assistant_edit_draft_restore_failed", "编辑消息附件恢复失败", {
          data: {
            messageId: message.id,
            addedCount: result.addedCount,
            expectedCount: result.expectedCount,
          },
          error: result.error,
        });
      } else {
        void frontendLog("INFO", "edit_composer_hydration_completed", "编辑草稿已写入输入框", {
          data: {
            messageId: message.id,
            addedCount: result.addedCount,
            removedCount: result.removedCount,
            expectedCount: result.expectedCount,
          },
        });
      }
      endEditComposerOperation(message.id, generation);
      if (draft.source === "recovery_override" && !result.error && !result.superseded) {
        // 释放恢复锁会触发 AUI 同步提交；最后一帧再写入，避免该提交把草稿重置为原消息文本。
        await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
        composer.setText(draft.text);
      }
      setIsHydrating(false);
    })();

    // 清理只做观测：草稿写入不再「卸载即取消」，因此重渲染不会中断它。
    return () => {
      void frontendLog("DEBUG", "edit_composer_hydration_cleanup", "编辑草稿 effect 清理（依赖变化或卸载）", {
        data: {
          messageId: message.id,
          addedCount: hydrationProgressRef.current.added,
          expectedCount: hydrationProgressRef.current.expected,
          aborted: hydrationProgressRef.current.aborted,
        },
      });
    };
  }, [aui.composer, message.id]);

  // 编辑框没有单独的埋点层，这里记录「取消编辑 / 重跑」点击与当时的草稿写入进度；
  // 纯观测，不干预按钮行为。
  useEffect(() => () => {
    void frontendLog("DEBUG", "edit_composer_unmounted", "编辑输入框已卸载", {
      data: {
        messageId: message.id,
        addedAttachmentCount: hydrationProgressRef.current.added,
        expectedAttachmentCount: hydrationProgressRef.current.expected,
        aborted: hydrationProgressRef.current.aborted,
      },
    });
  }, [message.id]);

  return (
    <MessagePrimitive.Root
      data-role="user"
      className="min-w-0 px-2"
      onClickCapture={(event) => {
        const label = (event.target as HTMLElement).closest<HTMLElement>("[aria-label]")?.getAttribute("aria-label");
        if (label !== "取消编辑" && label !== "重跑" && label !== "保存") return;
        void frontendLog("INFO", "edit_composer_action_clicked", "编辑框操作按钮被点击", {
          data: {
            action: label,
            isHydrating,
            addedAttachmentCount: hydrationProgressRef.current.added,
            expectedAttachmentCount: hydrationProgressRef.current.expected,
            aborted: hydrationProgressRef.current.aborted,
          },
        });
      }}
    >
      <ComposerInsertionProvider>
        <ComposerFeedbackProvider>
          <ComposerPrimitive.Root className={COMPOSER_SURFACE_CLASS_NAME}>
            <div className="min-h-0 min-w-0 w-full">
              <ComposerAttachments />
              <InlineComposerInput
                className={COMPOSER_EDITOR_CLASS_NAME}
                autoFocus
                hydrating={isHydrating || isRecoveryActive}
                submitDisabled={sendBlocked}
                aria-label="编辑消息"
              />
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 px-1">
              <div className="flex min-w-0 flex-1 items-center gap-1">
                <ThreadComposerPromptToolbar disabled={isHydrating || isRecoveryActive} lock={queuedEdit ? { kind: "queued" } : null} />
                <div className="min-w-0 flex-1">
                  {blockedReason !== null ? (
                    <span className="text-xs text-warning">
                      {blockedReason}
                      {canSwitchToRerun && (
                        <button
                          type="button"
                          className="ml-1 underline underline-offset-2 hover:text-foreground"
                          onClick={() => setIntent("rerun")}
                        >
                          改为编辑并重跑
                        </button>
                      )}
                    </span>
                  ) : queuedEdit ? (
                    <span className="text-muted-foreground text-xs">排队消息沿用发送时的配置</span>
                  ) : (
                    <ComposerControls
                      scope={{ kind: "task", id: taskId }}
                    />
                  )}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1.5">
                <ComposerAttachmentButton workspaceRoot={workspaceRoot} disabled={isHydrating || isRecoveryActive} />
                <ComposerPrimitive.Cancel render={<TooltipIconButton tooltip="取消编辑" aria-label="取消编辑" />}>
                  <XIcon />
                </ComposerPrimitive.Cancel>
                <ComposerSendButton blocked={sendBlocked} aria-label={queuedEdit ? "保存" : "重跑"}>
                  <ArrowUpIcon className="size-4" />
                </ComposerSendButton>
              </div>
            </div>
            <ComposerFeedback />
          </ComposerPrimitive.Root>
        </ComposerFeedbackProvider>
      </ComposerInsertionProvider>
    </MessagePrimitive.Root>
  );
};

const MessageError: FC = () => (
  <MessagePrimitive.Error>
    <ErrorPrimitive.Root className="border-destructive bg-destructive/10 text-destructive mt-2 rounded-md border p-3 text-sm">
      <ErrorPrimitive.Message className="line-clamp-2" />
    </ErrorPrimitive.Root>
  </MessagePrimitive.Error>
);

const AssistantMessage: FC = () => {
  const messageId = useAuiState((state) => state.message.id);
  const custom = useAuiState((state) => state.message.metadata.custom);
  // 渲染上下文随消息一并投影（见 frame store 的 contextFor），消息渲染只依赖消息自身事实；
  // 不回查 ``thread.state``，只读作用域也能正常渲染。
  const isRunEnd = custom?.isRunEnd === true;
  const runId = typeof custom?.runId === "number" ? custom.runId : null;
  const outcome = (custom?.outcome ?? null) as TransportOutcome | null;
  const activity = (custom?.activity ?? null) as TransportActivity | null;
  const { forkingMessageId = null, onForkMessage, cancelling = false, taskId, readonly = false } = useThreadContext();
  // fork 入口挂在每一轮回复的末尾，这一轮到终态（消息带 outcome）后才可用。
  const canFork = !readonly && isRunEnd && outcome !== null;
  const isForking = forkingMessageId === messageId;
  const usage = outcome?.usage ?? activity?.usage ?? null;
  const status = outcome?.status ?? activity?.status ?? null;

  return (
    <MessagePrimitive.Root
      data-role="assistant"
      className="relative -mb-6 pb-6 px-2 [content-visibility:auto] [contain-intrinsic-size:auto_24rem]"
    >
      <div className={cn("aui-assistant-part-stack text-foreground wrap-break-word", ASSISTANT_PART_STACK_CLASS)}>
        <MessagePrimitive.GroupedParts
          groupBy={assistantMessageGroupBy}
        >
          {({ part, children }) => {
            switch (part.type) {
              case "group-tool-trace": {
                return (
                  <RenderIsolationBoundary
                    scope="part"
                    resetKey={`tool-trace:${part.indices.join(",")}`}
                    metadata={{ renderer: "tool-trace-group", partIndices: part.indices }}
                    fallback={({ onRetry }) => (
                      <RenderErrorCard scope="part" label="工具分组" onRetry={onRetry} />
                    )}
                  >
                    <ToolTraceGroup indices={part.indices}>{children}</ToolTraceGroup>
                  </RenderIsolationBoundary>
                );
              }
              case "group-reasoning": {
                const isReasoningStreaming = part.status.type === "running";
                return (
                  <RenderIsolationBoundary
                    scope="part"
                    resetKey={`reasoning:${part.indices.join(",")}`}
                    metadata={{ renderer: "reasoning-group", partIndices: part.indices }}
                    fallback={({ onRetry }) => (
                      <RenderErrorCard scope="part" label="推理分组" onRetry={onRetry} />
                    )}
                  >
                    <ReasoningRoot streaming={isReasoningStreaming}>
                      <ReasoningTrigger active={isReasoningStreaming} />
                      <ReasoningContent aria-busy={isReasoningStreaming}>
                        <ReasoningText>{children}</ReasoningText>
                      </ReasoningContent>
                    </ReasoningRoot>
                  </RenderIsolationBoundary>
                );
              }
              case "text":
                return (
                  <PartRenderBoundary
                    renderer="Assistant Markdown"
                    fallback={({ onRetry }) => (
                      <RenderErrorCard
                        scope="markdown"
                        label="Assistant 内容"
                        fallbackText={part.text}
                        onRetry={onRetry}
                      />
                    )}
                  >
                    <MarkdownText status={part.status} />
                  </PartRenderBoundary>
                );
              case "reasoning":
                return (
                  <PartRenderBoundary
                    renderer="推理 Markdown"
                    fallback={({ onRetry }) => (
                      <RenderErrorCard
                        scope="markdown"
                        label="推理内容"
                        fallbackText={part.text}
                        onRetry={onRetry}
                      />
                    )}
                  >
                    <Reasoning {...part} />
                  </PartRenderBoundary>
                );
              case "tool-call":
                return (
                  <PartRenderBoundary
                    renderer="工具 renderer"
                    metadata={{ toolName: part.toolName }}
                    fallback={({ onRetry }) => (
                      <RenderErrorCard
                        scope="tool"
                        label={part.toolName}
                        onRetry={onRetry}
                      />
                    )}
                  >
                    <ToolPart
                      {...part}
                      taskId={taskId}
                      interactive={!readonly}
                      taskCancelling={cancelling}
                    />
                  </PartRenderBoundary>
                );
              case "file":
              case "image":
              case "data":
              case "source":
                return null;
              default:
                return null;
            }
          }}
        </MessagePrimitive.GroupedParts>
        <MessageError />
      </div>
      {!readonly && (
        <RunUsageDisplay usage={usage} running={activity !== null && outcome === null} visible={isRunEnd && (outcome !== null || activity !== null)} />
      )}
      <ActionBarPrimitive.Root className="mt-1 flex gap-1">
        <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip="复制" size="sm" />}>
          <AuiIf condition={(state) => state.message.isCopied}><CheckIcon /></AuiIf>
          <AuiIf condition={(state) => !state.message.isCopied}><CopyIcon /></AuiIf>
        </ActionBarPrimitive.Copy>
        {!readonly && isRunEnd && (
          <TooltipIconButton
            tooltip={canFork ? "从此处 Fork 新任务" : "此轮结束后才能 Fork"}
            size="sm"
            aria-label="从此处 Fork 新任务"
            disabled={!canFork || forkingMessageId !== null}
            onClick={() => onForkMessage?.(messageId)}
          >
            {isForking ? <Loader2Icon className="animate-spin" /> : <GitForkIcon />}
          </TooltipIconButton>
        )}
        <RunDiagnosticsActions
          taskId={taskId}
          runId={runId}
          status={status}
          endReason={outcome?.endReason ?? null}
          errorCode={outcome?.error?.code ?? null}
          visible={isRunEnd}
        />
      </ActionBarPrimitive.Root>
    </MessagePrimitive.Root>
  );
};

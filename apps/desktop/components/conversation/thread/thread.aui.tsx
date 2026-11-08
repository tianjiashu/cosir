import { editDraftKey, forgetComposerDraft, readComposerDraft, recoverComposerDraft, subscribeComposerDrafts, taskDraftKey } from "@/lib/assistant/composer-drafts";
import { useComposerDraft } from "@/components/composer/use-composer-draft";
import { GitForkIcon, ArrowDownIcon, ArrowUpIcon, CheckIcon, CopyIcon, Loader2Icon, PencilIcon, PlayIcon, Trash2Icon, XIcon, ZapIcon } from "lucide-react";
import { useContext, createContext, memo, Profiler, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type FC } from "react";
import {
  AuiIf,
  ActionBarPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  unstable_useComposerInput,
  useAui,
  useAuiState,
  type AssistantState,
} from "@assistant-ui/react";

import { StopButton } from "@/components/conversation-runtime/stop-button";
import { TaskContextUsage } from "@/components/conversation-runtime/usage-display";
import { ComposerControls } from "@/components/composer/composer-controls";
import { ComposerFeedback, ComposerFeedbackProvider } from "@/components/composer/composer-feedback";
import { ComposerPromptToolbar } from "@/components/composer/composer-prompt-toolbar";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import { CurrentActivity } from "@/components/conversation/turn/current-activity";
import { Turn } from "@/components/conversation/turn/turn";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import {
  deriveComposerAction,
  editBlockedReason,
  isEditableLatestUserMessage,
  isQueuedUserMessage,
  canResumeStoppedRun,
} from "@/lib/assistant/composer-action-policy";
import { readRuntimeTransportState } from "@/lib/assistant/transport-state-operations";
import { EDIT_SESSION_RUN_CONFIG, isContinuedEditSession, toEditableUserMessageDraft } from "@/lib/assistant/transport-message-converter";
import { editableDocumentAttachments } from "@/lib/assistant/editable-user-document";
import { isActiveRunStatus, type InputDelivery, type TransportActivity, type TransportMessageSource } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";
import type { ToolGroupCatalog } from "@/lib/api/tool-groups";
import type { TransportRunLaunch } from "@/lib/assistant/contract";
import { RUN_LAUNCH_LOCKED_HINT, useActiveRunLaunch } from "@/lib/assistant/run-launch";
import { AttachmentWorkspaceContext } from "@/components/conversation/thread/attachment-context";
import { VirtualizedThreadMessages } from "@/components/conversation/thread/virtualized-thread-messages";
import { ConversationAsideSlot } from "@/components/conversation/thread/conversation-aside";
import { QueuedMessages } from "@/components/conversation/thread/queued-messages";
import { AgentInboundMessage } from "@/components/conversation/thread/agent-message";
import { deleteQueuedTaskMessage, sendQueuedTaskMessageNow } from "@/lib/assistant/task-commands";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { UserMessageContent, useHasUserMessageContent } from "@/components/conversation/thread/user-message-content";
import { UserMessageImages } from "@/components/conversation/thread/user-message-images";
import {
  ComposerAttachmentButton,
  ComposerSendButton,
  ComposerAttachments,
  InlineComposerInput,
} from "@/components/conversation/thread/attachment.aui";
import { COMPOSER_EDITOR_CLASS_NAME, COMPOSER_PLACEHOLDER } from "@/components/composer/composer-editor";
import { COMPOSER_SURFACE_CLASS_NAME } from "@/components/composer/composer-surface";
import { ComposerInsertionProvider } from "@/components/composer/composer-insertion-context";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { notify } from "@/lib/notify";
import { LinkTaskContext } from "@/components/links/use-open-link";

export type ThreadProps = {
  /** 只渲染 canonical message UI，不提供写入操作。 */
  readonly?: boolean;
  taskId: number;
  workspaceId: number;
  toolGroups?: ToolGroupCatalog[];
  selectedToolGroups?: string[];
  onSelectedToolGroupsChange?: (groups: string[]) => void;
  toolGroupsLoading?: boolean;
  toolGroupsError?: string | null;
  workspaceRoot?: string;
  /** 正在分叉的回复 id。 */
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
  /** 可写对话必传（由 AssistantThreadSurface 提供）；只读对话不渲染 Composer，不需要。 */
  onResumeBusiness?: () => Promise<void>;
  /** 用户已请求停止 Task 的当前活动、尚未收敛（Task 级，不按 runId）。 */
  cancelling?: boolean;
  performanceProbe?: AssistantPerformanceProbe | null;
};

const RESUME_FEEDBACK_TIMEOUT_MS = 15_000;
type ThreadContextValue = Pick<ThreadProps, "forkingMessageId" | "onForkMessage" | "onResumeBusiness" | "workspaceRoot" | "cancelling" | "readonly" | "toolGroups" | "selectedToolGroups" | "onSelectedToolGroupsChange" | "toolGroupsLoading" | "toolGroupsError" | "taskId">;
const ThreadContext = createContext<ThreadContextValue | null>(null);

/** 线程内部组件只在 Thread 下渲染；缺 Provider 是装配错误，直接暴露。 */
function useThreadContext(): ThreadContextValue {
  const context = useContext(ThreadContext);
  if (!context) throw new Error("ThreadContext 只能在 Thread 内使用");
  return context;
}

const isNewChatView = (state: AssistantState) => state.thread.messages.length === 0;

export const Thread: FC<ThreadProps> = memo(function Thread({ readonly = false, taskId, workspaceId, toolGroups, selectedToolGroups, onSelectedToolGroupsChange, toolGroupsLoading, toolGroupsError, workspaceRoot, forkingMessageId = null, onForkMessage, onResumeBusiness, cancelling = false, performanceProbe = null }) {
  const isEmpty = useAuiState(isNewChatView);
  const messageComponents = useMemo(() => ({ Message: ThreadMessage }), []);
  return (
    <ThreadContext.Provider value={{ taskId, toolGroups, selectedToolGroups, onSelectedToolGroupsChange, toolGroupsLoading, toolGroupsError, workspaceRoot, forkingMessageId, onForkMessage, onResumeBusiness, cancelling, readonly }}>
      <AttachmentWorkspaceContext.Provider value={workspaceId}>
      <LinkTaskContext.Provider value={taskId}>
      <ThreadPrimitive.Root className="aui-root aui-thread-root bg-background flex h-full min-h-0 min-w-0 flex-col">
        {/*
          顶部锚点固定本轮用户消息，答案在其下方增长，流式 token 不会持续把滚动位置重新
          钉回底部。这里显式关闭 autoScroll，因为该模式默认值为 false；中途追踪底部会
          换取稳定视口。屏幕外消息只由虚拟列表跳过（见 VirtualizedThreadMessages）。
          视口不能加 CSS scroll-smooth：虚拟列表重新测量行高时会程序化修正 scrollTop，
          平滑滚动会把这些修正变成可见的漂移。
        */}
        <ForkOrigin />
        <ThreadPrimitive.Viewport turnAnchor="top" autoScroll={false} className="relative flex min-h-0 min-w-0 w-full flex-1 flex-col overflow-x-hidden overflow-y-auto">
            <div aria-hidden="true" className="scroll-fade-top" />
            {/* 内容列与对话附注同处一个网格，附注在滚动容器里、滚动条在最外侧（见 ConversationAsideSlot）。 */}
            <div className="grid min-w-0 flex-1 grid-cols-[minmax(0,1fr)_auto]">
              <div className={cn("col-start-1 row-start-1 mx-auto flex min-w-0 w-full max-w-conversation flex-col px-4 pt-6", isEmpty && "justify-center")}>
                <div className="mb-14">
                <VirtualizedThreadMessages
                  taskId={taskId}
                  components={messageComponents}
                  performanceProbe={performanceProbe}
                />
                <CurrentActivity cancelling={cancelling ?? false} />
                </div>
                {!readonly && (
                  <ThreadPrimitive.ViewportFooter className="bg-background sticky bottom-0 mt-auto flex min-w-0 flex-col gap-4 pb-4 md:pb-6">
                    <div aria-hidden="true" className="scroll-fade-bottom" />
                    <ScrollToBottomButton />
                    <QueuedMessages components={messageComponents} />
                    <ComposerBoundary performanceProbe={performanceProbe} />
                  </ThreadPrimitive.ViewportFooter>
                )}
              </div>
              <ConversationAsideSlot />
            </div>
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
      </LinkTaskContext.Provider>
      </AttachmentWorkspaceContext.Provider>
    </ThreadContext.Provider>
  );
});

/**
 * 「回到底部」按钮：仅在视口未贴底时可见（primitive 在贴底时禁用，样式上淡出）。
 * 刻意保持克制：不加运行中提示点，流式状态已由 composer 的停止按钮表达。
 * 轮廓用低透明度 ring 而非实色边框：浅色下靠柔和阴影浮起，深色下阴影不可见，靠 ring 保留轮廓。
 * 必须显式覆盖 TooltipIconButton 默认的 size-6 p-1，否则内边距会挤掉图标空间。
 * 不用平滑滚动：它要逐行穿过未测量的行，总高度一路在变，库每次变化都重发滚动、动画从头开始，长对话几秒
 * 都到不了底；运行中库又因顶部锚点取消重发，停在旧目标。即时跳过中间，只测量底部几行，一步到位。
 */
const ScrollToBottomButton: FC = () => (
  <ThreadPrimitive.ScrollToBottom
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
  performanceProbe,
}: {
  performanceProbe?: AssistantPerformanceProbe | null;
}) {
  const composer = <Composer />;
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

const Composer = memo(function Composer() {
  // 运行期配置锁定：活动 Run 期间模型与工具显示 Run 冻结的配置且只读，发送也用这份配置。
  const runLaunch = useActiveRunLaunch();
  // 运行中照常可以发送（后端排进队列，当前 Run 结束后依次处理）；只有停止请求在途时不提交，避免与停止交错。
  const { cancelling = false, taskId, workspaceRoot } = useThreadContext();
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
              <ThreadComposerPromptToolbar lockedLaunch={runLaunch} />
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
            <ComposerAction />
          </div>
          <ComposerFeedback />
        </ComposerPrimitive.Root>
      </ComposerFeedbackProvider>
    </ComposerInsertionProvider>
  );
});

const EMPTY_TOOL_GROUPS: ToolGroupCatalog[] = [];

/** `lockedLaunch` 为活动 Run 冻结的配置：工具组按它冻结的组名回显、只读。 */
const ThreadComposerPromptToolbar: FC<{ disabled?: boolean; lockedLaunch?: TransportRunLaunch | null }> = ({
  disabled = false,
  lockedLaunch = null,
}) => {
  const composer = unstable_useComposerInput();
  const context = useThreadContext();
  const toolGroups = context.toolGroups ?? EMPTY_TOOL_GROUPS;
  const lockedGroups = lockedLaunch?.disabledToolGroups ?? null;
  return (
    <ComposerPromptToolbar
      toolGroups={toolGroups}
      selectedToolGroups={lockedGroups ?? context.selectedToolGroups ?? []}
      onSelectedToolGroupsChange={lockedLaunch === null ? context.onSelectedToolGroupsChange : undefined}
      toolGroupsLoading={context.toolGroupsLoading}
      toolGroupsError={context.toolGroupsError}
      disabled={disabled || composer.isDisabled}
      toolGroupsLockedHint={lockedGroups === null ? undefined : RUN_LAUNCH_LOCKED_HINT}
    />
  );
};

const ComposerAction: FC = () => {
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const isDraftEmpty = useAuiState((state) =>
    state.composer.text.trim().length === 0 && state.composer.attachments.length === 0,
  );
  const { cancelling = false, onResumeBusiness, taskId } = useThreadContext();
  const canResume = useAuiState((state) => canResumeStoppedRun(readRuntimeTransportState(state.thread.state)));
  const isCancelling = cancelling;
  const action = deriveComposerAction({ isRunning, isDraftEmpty, canResume, isCancelling });
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
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
    return <StopButton taskId={taskId} />;
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
      {isRunning && <StopButton taskId={taskId} />}
      <ComposerSendButton
        aria-label={isRunning ? "加入队列" : "发送"}
        title={isRunning ? "加入队列：当前运行结束后依次处理，可在队列里改为立即发送" : undefined}
      >
        <ArrowUpIcon className="size-4" />
      </ComposerSendButton>
    </div>
  );
};

/** 排队条目的状态说明：`next_run` 等当前运行结束后依次处理；`immediate` 已改为立即处理、等待并入。 */
const QUEUED_LABELS: Record<InputDelivery, string> = {
  next_run: "排队中",
  immediate: "即将处理",
};

const ThreadMessage: FC = () => {
  const { taskId } = useThreadContext();
  const role = useAuiState((state) => state.message.role);
  const source = useAuiState((state) => messageSource(state.message.metadata.custom));
  // Agent 消息在这里就分流：它不能进入 UserMessage 的编辑态恢复与排队判定。
  if (source !== null) return <AgentInboundMessage source={source} taskId={taskId} />;
  return role === "user" ? <UserMessage /> : <AssistantMessage />;
};

/** 读 converter 放进 `metadata.custom` 的发送方；只有 Agent 消息非空。 */
function messageSource(custom: Record<string, unknown> | undefined): TransportMessageSource | null {
  const source = custom?.source;
  return source && typeof source === "object" ? (source as TransportMessageSource) : null;
}

const UserMessage: FC = () => {
  const aui = useAui();
  const messageId = useAuiState((state) => state.message.id);
  const isEditing = useAuiState((state) => state.composer.isEditing);
  const { taskId, readonly: readonlyThread } = useThreadContext();
  const readonly = readonlyThread === true;
  const draftKey = editDraftKey(taskId, messageId);
  const hasEditDraft = useSyncExternalStore(subscribeComposerDrafts, () => readComposerDraft(draftKey) !== undefined, () => false);
  // 有编辑草稿就是编辑中：切回 Task 或重跑被拒时，新的 runtime 里要重新进入编辑态。
  const enteringEdit = useRef(false);
  useEffect(() => {
    if (isEditing) enteringEdit.current = false;
    // effect 可能在进入编辑生效前重复执行（StrictMode），再次 beginEdit 会抛错，不能靠 composer 状态判断。
    if (!hasEditDraft || isEditing || readonly || enteringEdit.current) return;
    enteringEdit.current = true;
    try {
      aui.composer.beginEdit();
    } catch (error) {
      forgetComposerDraft(draftKey);
      void frontendLog("WARNING", "assistant_edit_composer_restore_rejected", "消息行未能重新进入编辑态", {
        data: { messageId },
        error,
      });
    }
  }, [aui, draftKey, hasEditDraft, isEditing, messageId, readonly]);
  const queued = useAuiState((state) => isQueuedUserMessage(readRuntimeTransportState(state.thread.state), messageId));
  if (isEditing && !readonly) return <UserEditMessage />;
  // 排队消息只在输入框上方的队列区渲染（见 QueuedMessages），消息流里已滤掉。
  return queued ? <QueuedMessageView /> : <UserMessageView />;
};

const UserMessageView: FC = () => {
  const messageId = useAuiState((state) => state.message.id);
  const pending = useAuiState((state) => state.message.metadata.custom?.pending === true);
  const hasContent = useHasUserMessageContent();
  // 渲染失败时的纯文本兜底：只拼正文，胶囊所在位置留空。
  const userText = useAuiState((state) => state.message.parts.map((part) => part.type === "text" ? part.text : "").join(""));
  const readonly = useThreadContext().readonly === true;
  const canEdit = useAuiState((state) => {
    if (readonly) return false;
    const transportState = readRuntimeTransportState(state.thread.state);
    if (!isEditableLatestUserMessage(transportState, messageId)) return false;

    // Transport runtime 可能在 canonical snapshot 到达前乐观渲染新的用户命令；
    // 此期间不能因为旧 snapshot 仍在，就把上一条用户消息重新标记为可编辑。排队消息不算：
    // 它们排在已并入的消息之后，但不妨碍编辑重跑最新一条已并入的消息。
    const latestRenderedUserMessageId = [...state.thread.messages]
      .reverse()
      .find((message) => message.role === "user" && !isQueuedUserMessage(transportState, message.id))?.id;
    return latestRenderedUserMessageId === messageId;
  });

  return (
    <MessagePrimitive.Root
      data-role="user"
      className="group flex flex-col items-end px-2"
    >
      <div className="flex max-w-[85%] flex-col items-end gap-2">
        <UserMessageImages />
        {hasContent && (
          <div className="bg-foreground text-background rounded-[22px] px-4 py-3 text-chat leading-relaxed wrap-break-word">
            {/* 正文与行内胶囊是一个整体渲染单元（见 UserMessageContent），按消息隔离而不是按 part。 */}
            <RenderIsolationBoundary
              scope="message"
              resetKey={messageId}
              metadata={{ messageId, renderer: "用户 Markdown" }}
              fallback={({ onRetry }) => (
                <RenderErrorCard
                  scope="markdown"
                  label="用户内容"
                  fallbackText={userText}
                  onRetry={onRetry}
                />
              )}
            >
              <UserMessageContent />
            </RenderIsolationBoundary>
          </div>
        )}
      </div>
      <div className="mt-1 h-6 shrink-0">
        {pending ? <span role="status" className="text-muted-foreground text-xs leading-6">正在发送</span> : <ActionBarPrimitive.Root
          className="flex h-6 gap-1 opacity-0 transition-opacity pointer-events-none group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100"
        >
          {canEdit && (
            <ActionBarPrimitive.Edit render={<TooltipIconButton tooltip="编辑并重跑" aria-label="编辑并重跑" size="sm" />}>
              <PencilIcon />
            </ActionBarPrimitive.Edit>
          )}
          <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip="复制" aria-label="复制" size="sm" />}>
            <AuiIf condition={(state) => state.message.isCopied}><CheckIcon /></AuiIf>
            <AuiIf condition={(state) => !state.message.isCopied}><CopyIcon /></AuiIf>
          </ActionBarPrimitive.Copy>
        </ActionBarPrimitive.Root>}
      </div>
    </MessagePrimitive.Root>
  );
};

/** 写入 effect 需要的一次性草稿快照；与 message 对象解耦，避免写入被 transport 更新打断。 */
type QueuedCommand = "send_now" | "edit" | "delete";

const QUEUED_COMMAND_FAILURE: Record<QueuedCommand, string> = {
  send_now: "立即发送失败",
  edit: "编辑失败",
  delete: "删除失败",
};

/**
 * 队列区里的一条排队消息：立即发送、编辑、删除。
 *
 * 编辑 = 撤回这一条 + 把正文与附件放回输入框（排在输入框已有内容之前），改完照常发送、重新排队；撤回
 * 失败（已被并入）时不动输入框。命令只投递，结果以 snapshot 的 `queued_messages` 为准：立即发送后
 * 条目以新身份重新入队或直接被并入，撤回后条目消失。消息恰好已被并入时后端 409，消费投影随即
 * 把它移出队列区。
 */
const QueuedMessageView: FC = () => {
  const message = useAuiState((state) => state.message);
  const messageId = message.id;
  const hasContent = useHasUserMessageContent();
  const delivery = useAuiState((state) => readRuntimeTransportState(state.thread.state)?.queued_messages
    ?.find((message) => message.id === messageId)?.delivery ?? "next_run");
  const { taskId } = useThreadContext();
  const [busy, setBusy] = useState<QueuedCommand | null>(null);

  const moveToComposer = async () => {
    // 撤回前取草稿：撤回成功后条目随快照消失，消息作用域也跟着卸载。
    const draft = message.role === "user" ? toEditableUserMessageDraft(message) : null;
    await deleteQueuedTaskMessage(taskId, messageId);
    if (!draft) return;
    const result = await recoverComposerDraft(taskDraftKey(taskId), {
      text: draft.text,
      attachments: editableDocumentAttachments(draft.document),
    });
    if (result?.error) {
      void frontendLog("ERROR", "queued_message_composer_restore_failed", "排队消息放回输入框时附件恢复失败", {
        data: { taskId, messageId, addedCount: result.addedCount, expectedCount: result.expectedCount },
        error: result.error,
      });
    }
  };

  const run = async (command: QueuedCommand) => {
    if (busy !== null) return;
    setBusy(command);
    try {
      if (command === "send_now") await sendQueuedTaskMessageNow(taskId, messageId);
      else if (command === "edit") await moveToComposer();
      else await deleteQueuedTaskMessage(taskId, messageId);
      void frontendLog("INFO", "queued_message_command_accepted", "排队消息命令已受理", {
        data: { taskId, messageId, command },
      });
    } catch (error) {
      notify.error(QUEUED_COMMAND_FAILURE[command], { error, fallback: "这条消息可能已开始处理，请以对话内容为准" });
      void frontendLog("WARNING", "queued_message_command_failed", "排队消息命令失败", {
        data: { taskId, messageId, command },
        error,
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <MessagePrimitive.Root
      data-role="user"
      data-queued={delivery}
      className="bg-muted/40 flex items-start gap-2 rounded-2xl px-3 py-2"
    >
      <div className="min-w-0 flex-1">
        <span className="text-muted-foreground text-xs">{QUEUED_LABELS[delivery]}</span>
        <UserMessageImages />
        {hasContent && (
          <div className="text-foreground line-clamp-3 text-sm leading-relaxed wrap-break-word">
            <UserMessageContent />
          </div>
        )}
      </div>
      <ActionBarPrimitive.Root className="flex shrink-0 gap-1">
        {delivery === "next_run" && (
          <TooltipIconButton
            tooltip="立即发送"
            aria-label="立即发送"
            size="sm"
            disabled={busy !== null}
            onClick={() => void run("send_now")}
          >
            {busy === "send_now" ? <Loader2Icon className="animate-spin" /> : <ZapIcon />}
          </TooltipIconButton>
        )}
        <TooltipIconButton
          tooltip="编辑（放回输入框）"
          aria-label="编辑"
          size="sm"
          disabled={busy !== null}
          onClick={() => void run("edit")}
        >
          {busy === "edit" ? <Loader2Icon className="animate-spin" /> : <PencilIcon />}
        </TooltipIconButton>
        <TooltipIconButton
          tooltip="删除"
          aria-label="删除"
          size="sm"
          disabled={busy !== null}
          onClick={() => void run("delete")}
        >
          {busy === "delete" ? <Loader2Icon className="animate-spin" /> : <Trash2Icon />}
        </TooltipIconButton>
      </ActionBarPrimitive.Root>
    </MessagePrimitive.Root>
  );
};

const UserEditMessage: FC = () => {
  const aui = useAui();
  const message = useAuiState((state) => state.message);
  const { taskId, workspaceRoot } = useThreadContext();
  const transportState = useAuiState((state) => readRuntimeTransportState(state.thread.state));
  // 编辑会话可能在消息行重新挂载后继续（见 EDIT_SESSION_RUN_CONFIG）：composer 还在，不用恢复。
  const composerRunConfig = useAuiState((state) => state.composer.runConfig);
  const [continued] = useState(() => isContinuedEditSession(composerRunConfig));
  // 编辑期间前提失效（已不是可重跑的最新一条、Run 又跑起来了）时给出原因并禁止提交。
  const blockedReason = editBlockedReason(transportState, message.id);
  useLayoutEffect(() => {
    aui.composer.setRunConfig(EDIT_SESSION_RUN_CONFIG);
  }, [aui.composer]);
  const draftKey = editDraftKey(taskId, message.id);
  // AUI 把文件和图片都提成附件且丢了在正文里的位置，首次进入也要用还原后的内容替换。
  const [seed] = useState(() => {
    if (message.role !== "user") return null;
    const draft = toEditableUserMessageDraft(message);
    return { text: draft.text, attachments: editableDocumentAttachments(draft.document) };
  });
  const restoring = useComposerDraft(draftKey, { seed, continued });
  // 草稿先于退出编辑结束，否则消息行会以为这次编辑被打断，立刻重新进入。提交失败时由恢复写回。
  const endEdit = () => forgetComposerDraft(draftKey);
  // 回车与保存按钮共用这一个开关：任何「此刻不能提交」的条件只在这里加一次。
  const sendBlocked = restoring || blockedReason !== null;

  return (
    <MessagePrimitive.Root
      data-role="user"
      className="min-w-0 px-2"
    >
      <ComposerInsertionProvider>
        <ComposerFeedbackProvider>
          <ComposerPrimitive.Root className={COMPOSER_SURFACE_CLASS_NAME}>
            <div className="min-h-0 min-w-0 w-full">
              <ComposerAttachments />
              <InlineComposerInput
                className={COMPOSER_EDITOR_CLASS_NAME}
                autoFocus
                hydrating={restoring}
                submitDisabled={sendBlocked}
                beforeSubmit={endEdit}
                aria-label="编辑消息"
              />
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 px-1">
              <div className="flex min-w-0 flex-1 items-center gap-1">
                <ThreadComposerPromptToolbar disabled={restoring} />
                <div className="min-w-0 flex-1">
                  {blockedReason !== null ? (
                    <span className="text-xs text-warning">{blockedReason}</span>
                  ) : (
                    <ComposerControls
                      scope={{ kind: "task", id: taskId }}
                    />
                  )}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1.5">
                <ComposerAttachmentButton workspaceRoot={workspaceRoot} disabled={restoring} />
                <ComposerPrimitive.Cancel onClick={endEdit} render={<TooltipIconButton tooltip="取消编辑" aria-label="取消编辑" />}>
                  <XIcon />
                </ComposerPrimitive.Cancel>
                <ComposerSendButton blocked={sendBlocked} beforeSubmit={endEdit} aria-label="重跑">
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

/**
 * 助手消息 = 一个回合（`Turn`）：分区、开合与脚注都在回合组件里，这里只接上线程级上下文。
 * 渲染上下文（`isRunEnd` / `outcome` / `activity`）随消息一并投影（见 frame store 的 contextFor），消息渲染
 * 只依赖消息自身事实；不回查 `thread.state`，只读作用域也能正常渲染。
 */
const AssistantMessage: FC = () => {
  const { cancelling = false, taskId, readonly = false } = useThreadContext();
  return (
    <MessagePrimitive.Root data-role="assistant" className="relative -mb-6 px-2 pb-6">
      <div className="text-foreground wrap-break-word">
        <Turn taskId={taskId} readonly={readonly} cancelling={cancelling} footerAction={<ForkReplyAction />} />
      </div>
    </MessagePrimitive.Root>
  );
};

/** 来源是 Task 元数据，不占消息位置、不进入模型输入。 */
function ForkOrigin() {
  const fork = useAuiState((state) => readRuntimeTransportState(state.thread.state)?.fork);
  if (!fork) return null;
  return <div data-slot="fork-origin" className="text-muted-foreground mx-auto w-full max-w-conversation px-6 py-2 text-xs">
    继承自「{fork.sourceTitle}」· {fork.taskType === "agent_fork" ? "截至上一条回复 · 历史仅供上下文使用" : "截至所选回复"}
  </div>;
}

/** 从这条回复往后分叉；还在进行的那条回复没有入口，后端同样会拒绝。 */
function ForkReplyAction() {
  const { readonly, onForkMessage, forkingMessageId } = useThreadContext();
  const id = useAuiState((state) => state.message.id);
  const inProgress = useAuiState((state) => isActiveRunStatus((state.message.metadata.custom?.activity as TransportActivity | null | undefined)?.status));
  if (readonly || !onForkMessage || inProgress) return null;
  return <TooltipIconButton tooltip="从这里分叉：开一个新对话，当前对话保持不变" aria-label="从这里分叉" size="sm"
    disabled={forkingMessageId != null} onClick={() => onForkMessage(id)}>
    {forkingMessageId === id ? <Loader2Icon className="animate-spin" /> : <GitForkIcon />}
  </TooltipIconButton>;
}

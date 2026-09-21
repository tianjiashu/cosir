"use client";

import { ArrowDownIcon, ArrowUpIcon, CheckIcon, CopyIcon, GitForkIcon, Loader2Icon, PencilIcon, PlayIcon, XIcon } from "lucide-react";
import { useContext, createContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type ComponentType, type FC, type ReactNode } from "react";
import {
  AuiIf,
  ActionBarPrimitive,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
  type AssistantState,
} from "@assistant-ui/react";

import { StopButton } from "@/components/assistant/stop-button";
import { RunUsageDisplay, TaskContextUsage } from "@/components/assistant/usage-display";
import { ComposerControls } from "@/components/composer/composer-controls";
import { MarkdownText } from "@/components/markdown-text";
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
import { ToolPart } from "@/components/assistant-ui/tools/tool-part";
import { readToolArtifact } from "@/components/assistant-ui/tools/types";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import {
  deriveComposerAction,
  getTransportRunId,
  isEditableLatestRunUserMessage,
  isResumableCancelledRun,
} from "@/lib/assistant/conversation-actions";
import { toEditableUserMessageDraft } from "@/lib/assistant/converter";
import { localFileTokenIds } from "@/lib/assistant/attachments/local-file-token";
import { editableDocumentAttachments } from "@/lib/assistant/editable-user-document";
import {
  applyEditDraftToComposer,
  beginEditComposerOperation,
  currentEditComposerOperation,
  editableDraftAttachments,
  endEditComposerOperation,
  isCurrentEditComposerOperation,
  isEditComposerOperationActive,
  subscribeEditComposerOperations,
  takePendingEditComposerDraft,
  type EditDraftAttachment,
} from "@/lib/assistant/edit-composer-draft";
import type { TransportState, TransportToolStatus } from "@/lib/assistant/contract";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";
import { AttachmentTaskContext } from "@/components/assistant-ui/elements/attachment-context";
import { VirtualizedThreadMessages } from "@/components/assistant-ui/elements/virtualized-thread-messages";
import { UserMessageAttachments } from "@/components/assistant-ui/elements/user-message-attachments";
import {
  ComposerAttachmentButton,
  ComposerAttachments,
  InlineComposerInput,
} from "@/components/assistant-ui/elements/attachment.aui";
import { InlineAttachmentInsertionProvider } from "@/components/composer/inline-attachment-input";

export type ThreadComponents = {
  AssistantMessage?: ComponentType;
  Welcome?: ComponentType;
};

export type ThreadProps = {
  components?: ThreadComponents;
  autoFocus?: boolean;
  /** Render the canonical message UI without any write affordances. */
  readonly?: boolean;
  taskId?: number;
  workspaceRoot?: string;
  forkAvailable?: boolean;
  forkingRunId?: number | null;
  onForkRun?: (runId: number) => void;
  onResumeBusiness?: () => Promise<void>;
  onCancelRequested?: (runId: number) => void;
  onCancelResult?: (runId: number, accepted: boolean) => void;
  cancellingRunId?: number | null;
};

const EMPTY_COMPONENTS: ThreadComponents = {};
const RESUME_FEEDBACK_TIMEOUT_MS = 15_000;
const ThreadComponentsContext = createContext<ThreadComponents>(EMPTY_COMPONENTS);
type ThreadContextValue = Pick<ThreadProps, "forkAvailable" | "forkingRunId" | "onForkRun" | "onResumeBusiness" | "onCancelRequested" | "onCancelResult" | "workspaceRoot" | "cancellingRunId" | "readonly"> & { taskId?: number };
const ThreadContext = createContext<ThreadContextValue>({});

type AssistantGroupKey = "group-reasoning" | "group-tool-trace";

type ToolTraceGroupProps = {
  indices: readonly number[];
  children: ReactNode;
};

/**
 * Render a grouped tool trace using each tool's backend lifecycle status.
 *
 * assistant-ui derives a tool part's standard status from the assistant
 * message status when no tool result is present. That makes completed tools
 * appear running while the assistant continues with a later step, so this
 * component deliberately reads the transport artifact for the aggregate.
 */
const ToolTraceGroup: FC<ToolTraceGroupProps> = ({ indices, children }) => {
  const statusKey = useAuiState((state) => indices.map((index) => {
    const part = state.message.parts[index];
    if (part?.type !== "tool-call") return "unknown";
    return readToolArtifact(part.artifact).backendStatus;
  }).join("|"));
  const summary = useMemo(
    () => summarizeToolGroup(
      (statusKey ? statusKey.split("|") : []) as TransportToolStatus[],
    ),
    [statusKey],
  );

  return (
    <ToolGroupRoot variant="ghost">
      <ToolGroupTrigger count={summary.total} summary={summary} />
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

export const Thread: FC<ThreadProps> = ({ components = EMPTY_COMPONENTS, autoFocus = true, readonly = false, taskId, workspaceRoot, forkAvailable = false, forkingRunId = null, onForkRun, onResumeBusiness, onCancelRequested, onCancelResult, cancellingRunId = null }) => {
  const isEmpty = useAuiState(isNewChatView);
  const viewportRef = useRef<HTMLDivElement>(null);
  const messageComponents = useMemo(() => ({ Message: ThreadMessage }), []);
  return (
    <ThreadContext.Provider value={{ taskId, workspaceRoot, forkAvailable, forkingRunId, onForkRun, onResumeBusiness, onCancelRequested, onCancelResult, cancellingRunId, readonly }}>
    <ThreadComponentsContext.Provider value={components}>
      <AttachmentTaskContext.Provider value={taskId}>
      <ThreadPrimitive.Root className="aui-root aui-thread-root bg-background flex h-full min-h-0 min-w-0 flex-col">
        {/*
          Top anchor pins the turn's user message while the answer grows below
          it, so streaming no longer re-pins the scroll position on every
          chunk. autoScroll is stated explicitly because it defaults to false
          in this mode: following the bottom mid-stream is deliberately traded
          for a stable viewport. Message roots below additionally skip
          off-screen layout and paint through content-visibility utilities
          (intrinsic size is not calibrated yet).
        */}
        <ThreadPrimitive.Viewport ref={viewportRef} turnAnchor="top" autoScroll={false} className="relative flex min-h-0 min-w-0 w-full flex-1 flex-col overflow-x-hidden overflow-y-auto scroll-smooth">
            <div className={cn("mx-auto flex min-w-0 w-full max-w-3xl flex-1 flex-col px-4 pt-4", isEmpty && "justify-center")}>
              <VirtualizedThreadMessages
                scrollElementRef={viewportRef}
                components={messageComponents}
                rowPaddingBottom="1.5rem"
                keepActiveTail
              />
              {!readonly && (
                <ThreadPrimitive.ViewportFooter className={cn("bg-background sticky bottom-0 mt-auto flex min-w-0 flex-col gap-4 pb-4 md:pb-6", !isEmpty && "rounded-t-3xl")}>
                  <ThreadPrimitive.ScrollToBottom
                    render={<TooltipIconButton tooltip="回到底部" variant="outline" className="absolute -top-12 self-center rounded-full p-3 disabled:invisible" />}
                  >
                    <ArrowDownIcon />
                  </ThreadPrimitive.ScrollToBottom>
                  <Composer autoFocus={autoFocus} taskId={taskId} workspaceRoot={workspaceRoot} />
                </ThreadPrimitive.ViewportFooter>
              )}
            </div>
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
      </AttachmentTaskContext.Provider>
    </ThreadComponentsContext.Provider>
    </ThreadContext.Provider>
  );
};

const Composer: FC<{ autoFocus: boolean; taskId?: number; workspaceRoot?: string }> = ({ autoFocus, taskId, workspaceRoot }) => (
  <InlineAttachmentInsertionProvider>
    <ComposerPrimitive.Root className="border-border/60 bg-card flex min-w-0 w-full flex-col gap-2 rounded-3xl border p-2 shadow-sm">
      <ComposerPrimitive.AttachmentDropzone className="min-h-0 min-w-0 w-full">
        <ComposerAttachments />
        <InlineComposerInput
          placeholder="输入任务，例如：帮我查找登录相关代码…"
          className="text-foreground placeholder:text-muted-foreground/60 max-h-48 min-h-20 w-full resize-none bg-transparent px-2.5 py-1 text-base leading-6 outline-none"
          autoFocus={autoFocus}
          aria-label="消息输入"
        />
      </ComposerPrimitive.AttachmentDropzone>
      <div className="flex flex-wrap items-center justify-between gap-2 px-1">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
          <TaskContextUsage />
          <div className="min-w-0 flex-1">
            <ComposerControls
              scope={taskId == null ? undefined : { kind: "task", id: taskId }}
              runtimeModelContext
            />
          </div>
        </div>
        <ComposerAttachmentButton workspaceRoot={workspaceRoot} />
        <ComposerAction taskId={taskId ?? null} />
      </div>
    </ComposerPrimitive.Root>
  </InlineAttachmentInsertionProvider>
);

const ComposerAction: FC<{ taskId: number | null }> = ({ taskId }) => {
  const aui = useAui();
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const draftLength = useAuiState((state) => state.composer.text.length);
  const isDraftEmpty = useAuiState((state) =>
    state.composer.text.trim().length === 0 && state.composer.attachments.length === 0,
  );
  const { cancellingRunId, onResumeBusiness, onCancelRequested, onCancelResult } = useContext(ThreadContext);
  const runId = useAuiState((state) => getTransportRunId(state.thread.state));
  const canResume = useAuiState((state) => isResumableCancelledRun(state.thread.state as unknown as TransportState));
  const isCancelling = cancellingRunId !== null && cancellingRunId === runId;
  const action = deriveComposerAction({ isRunning, isDraftEmpty, canResume, isCancelling });
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);

  useEffect(() => {
    void frontendLog("DEBUG", "composer_action_derived", "Composer action 状态发生变化", {
      data: { taskId, action, isRunning, isDraftEmpty, canResume, isCancelling, cancellingRunId, runId, draftLength },
    });
  }, [action, canResume, cancellingRunId, draftLength, isCancelling, isDraftEmpty, isRunning, runId, taskId]);

  useEffect(() => {
    if (isRunning || !canResume) {
      setResuming(false);
      setResumeError(null);
    }
  }, [canResume, isRunning]);

  useEffect(() => {
    if (!resuming) return;
    const timeout = window.setTimeout(() => {
      setResuming(false);
      setResumeError("继续运行请求未建立连接，请重试");
    }, RESUME_FEEDBACK_TIMEOUT_MS);
    return () => window.clearTimeout(timeout);
  }, [resuming]);

  if (action === "stop") {
    return <ComposerPrimitive.Cancel render={<StopButton taskId={taskId} onCancelRequested={onCancelRequested} onCancelResult={onCancelResult} />} />;
  }

  if (action === "cancelling") {
    return <StopButton taskId={taskId} isCancelling />;
  }

  if (action === "resume") {
    const resume = async () => {
      if (resuming) return;
      setResuming(true);
      setResumeError(null);
      try {
        if (onResumeBusiness) {
          await onResumeBusiness();
        } else {
          await Promise.resolve(aui.thread.resumeRun({ parentId: null }));
        }
      } catch (error) {
        setResuming(false);
        setResumeError(safeFrontendErrorMessage(error, "继续运行失败，请检查本机后端状态"));
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
    <ComposerPrimitive.Send render={<Button type="button" size="icon" className="size-8 rounded-full" aria-label="发送" />}>
      <ArrowUpIcon className="size-4" />
    </ComposerPrimitive.Send>
  );
};

const ThreadMessage: FC = () => {
  const role = useAuiState((state) => state.message.role);
  const { AssistantMessage = AssistantMessageDefault } = useContext(ThreadComponentsContext);
  return role === "user" ? <UserMessage /> : <AssistantMessage />;
};

const UserMessage: FC = () => {
  const isEditing = useAuiState((state) => state.composer.isEditing);
  const readonly = useContext(ThreadContext).readonly === true;
  if (isEditing && !readonly) return <UserEditMessage />;
  return <UserMessageView />;
};

const UserMessageView: FC = () => {
  const messageId = useAuiState((state) => state.message.id);
  const hasText = useAuiState((state) => state.message.parts.some(
    (part) => part.type === "text" && part.text.trim().length > 0,
  ));
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const readonly = useContext(ThreadContext).readonly === true;
  const canEdit = useAuiState((state) => {
    if (readonly || isRunning || !isEditableLatestRunUserMessage(
      state.thread.state as unknown as TransportState,
      messageId,
    )) return false;

    // The transport runtime may render a new user command optimistically
    // before its canonical snapshot arrives. During that window the old
    // snapshot must not make the previous user message editable again.
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
        <UserMessageAttachments />
        {hasText && (
          <div className="bg-primary/10 text-foreground rounded-2xl px-4 py-2.5 text-sm leading-relaxed wrap-break-word">
            <MessagePrimitive.Parts>{({ part }) => (
              part.type === "text" ? <MarkdownText status={part.status} /> : null
            )}</MessagePrimitive.Parts>
          </div>
        )}
      </div>
      <div className="mt-1 h-6 shrink-0">
        <ActionBarPrimitive.Root
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
  const { taskId, workspaceRoot } = useContext(ThreadContext);
  const [restoreOverride] = useState(() => takePendingEditComposerDraft(message.id));
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
  // 草稿写入进度，仅供日志复盘；不参与渲染或业务判断。
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
    hydrationProgressRef.current = { expected: expectedAttachments, added: 0, aborted: null };
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

    // 唯一写入实现：先补齐附件、再写文本、最后清理草稿之外的附件。任何中断点都只会
    // 「多留附件」，不会留下「文本含 token 而附件缺失」的空窗。
    void (async () => {
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
            pendingAttachmentIds: draft.attachments
              .slice(result.addedCount)
              .map((pending) => pending.id),
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
        if (label !== "取消编辑" && label !== "重跑") return;
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
      <InlineAttachmentInsertionProvider>
        <ComposerPrimitive.Root className="border-border/60 bg-card flex min-w-0 w-full flex-col gap-2 rounded-3xl border p-2 shadow-sm">
          <ComposerPrimitive.AttachmentDropzone className="min-h-0 min-w-0 w-full">
            <ComposerAttachments />
            <InlineComposerInput
              className="text-foreground placeholder:text-muted-foreground/60 max-h-48 min-h-20 w-full resize-none bg-transparent px-2.5 py-1 text-base leading-6 outline-none"
              autoFocus
              suspendAttachmentReconciliation={isHydrating || isRecoveryActive}
              aria-label="编辑消息"
            />
          </ComposerPrimitive.AttachmentDropzone>
          <div className="flex flex-wrap items-center justify-between gap-2 px-1">
            <div className="min-w-0 flex-1">
              <ComposerControls
                scope={taskId == null ? undefined : { kind: "task", id: taskId }}
              />
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <ComposerAttachmentButton workspaceRoot={workspaceRoot} disabled={isHydrating || isRecoveryActive} />
              <ComposerPrimitive.Cancel render={<TooltipIconButton tooltip="取消编辑" aria-label="取消编辑" />}>
                <XIcon />
              </ComposerPrimitive.Cancel>
              <ComposerPrimitive.Send render={<Button type="button" size="icon" className="size-8 rounded-full" aria-label="重跑" disabled={isHydrating || isRecoveryActive} />}>
                <ArrowUpIcon className="size-4" />
              </ComposerPrimitive.Send>
            </div>
          </div>
        </ComposerPrimitive.Root>
      </InlineAttachmentInsertionProvider>
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

const AssistantMessageDefault: FC = () => {
  const custom = useAuiState((state) => state.message.metadata.custom);
  const isLastRunMessage = custom?.isLastRunMessage === true;
  const runId = typeof custom?.runId === "number" ? custom.runId : null;
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const { forkAvailable = false, forkingRunId = null, onForkRun, cancellingRunId = null, taskId, readonly = false } = useContext(ThreadContext);
  const canFork = !readonly && isLastRunMessage && runId !== null && forkAvailable && !isRunning;
  const isForking = runId !== null && forkingRunId === runId;

  return (
    <MessagePrimitive.Root
      data-role="assistant"
      className="relative -mb-6 pb-6 px-2 [content-visibility:auto] [contain-intrinsic-size:auto_24rem]"
    >
      <div className="text-foreground leading-relaxed wrap-break-word">
        <MessagePrimitive.GroupedParts
          groupBy={assistantMessageGroupBy}
        >
          {({ part, children }) => {
            switch (part.type) {
              case "group-tool-trace": {
                return <ToolTraceGroup indices={part.indices}>{children}</ToolTraceGroup>;
              }
              case "group-reasoning": {
                const isReasoningStreaming = part.status.type === "running";
                return (
                  <ReasoningRoot variant="ghost" streaming={isReasoningStreaming}>
                    <ReasoningTrigger active={isReasoningStreaming} />
                    <ReasoningContent aria-busy={isReasoningStreaming}>
                      <ReasoningText>{children}</ReasoningText>
                    </ReasoningContent>
                  </ReasoningRoot>
                );
              }
              case "text":
                return <MarkdownText status={part.status} />;
              case "reasoning":
                return <Reasoning {...part} />;
              case "tool-call":
                return <ToolPart {...part} taskId={taskId} runId={runId} runCancelling={cancellingRunId === runId} />;
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
      {!readonly && <RunUsageDisplay runId={runId} visible={isLastRunMessage} />}
      <ActionBarPrimitive.Root hideWhenRunning className="mt-1 flex gap-1">
        <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip="复制" size="sm" />}>
          <AuiIf condition={(state) => state.message.isCopied}><CheckIcon /></AuiIf>
          <AuiIf condition={(state) => !state.message.isCopied}><CopyIcon /></AuiIf>
        </ActionBarPrimitive.Copy>
        {!readonly && isLastRunMessage && runId !== null && (
          <TooltipIconButton
            tooltip={forkAvailable ? "从此处 Fork 新任务" : "所有 Run 完成后才能 Fork"}
            size="sm"
            aria-label="从此处 Fork 新任务"
            disabled={!canFork || isForking}
            onClick={() => onForkRun?.(runId)}
          >
            {isForking ? <Loader2Icon className="animate-spin" /> : <GitForkIcon />}
          </TooltipIconButton>
        )}
      </ActionBarPrimitive.Root>
    </MessagePrimitive.Root>
  );
};

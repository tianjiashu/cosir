"use client";

import { ArrowDownIcon, ArrowUpIcon, CheckIcon, CopyIcon, GitForkIcon, Loader2Icon, PencilIcon, PlayIcon, XIcon } from "lucide-react";
import { useContext, createContext, useEffect, useState, type FC, type ComponentType } from "react";
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
import { ToolPart } from "@/components/assistant-ui/tools/tool-part";
import { readToolArtifact } from "@/components/assistant-ui/tools/types";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import {
  deriveComposerAction,
  isEditableLatestRunUserMessage,
  isResumableCancelledRun,
} from "@/lib/assistant/conversation-actions";
import type { TransportState } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";

export type ThreadComponents = {
  AssistantMessage?: ComponentType;
  Welcome?: ComponentType;
};

export type ThreadProps = {
  components?: ThreadComponents;
  autoFocus?: boolean;
  taskId?: number;
  forkAvailable?: boolean;
  forkingRunId?: number | null;
  onForkRun?: (runId: number) => void;
  onResumeBusiness?: () => Promise<void>;
};

const EMPTY_COMPONENTS: ThreadComponents = {};
const RESUME_FEEDBACK_TIMEOUT_MS = 15_000;
const ThreadComponentsContext = createContext<ThreadComponents>(EMPTY_COMPONENTS);
type ThreadContextValue = Pick<ThreadProps, "forkAvailable" | "forkingRunId" | "onForkRun" | "onResumeBusiness"> & { taskId?: number };
const ThreadContext = createContext<ThreadContextValue>({});

type AssistantGroupKey = "group-reasoning" | "group-tool-trace";

const assistantMessageGroupBy = (
  part: { type: string; artifact?: unknown },
): readonly AssistantGroupKey[] => {
  if (part.type === "reasoning") return ["group-reasoning"];
  if (part.type !== "tool-call") return [];
  const surface = readToolArtifact(part.artifact).presentation.surface;
  return surface === "standalone" ? [] : ["group-tool-trace"];
};

const isNewChatView = (state: AssistantState) => state.thread.messages.length === 0;

export const Thread: FC<ThreadProps> = ({ components = EMPTY_COMPONENTS, autoFocus = true, taskId, forkAvailable = false, forkingRunId = null, onForkRun, onResumeBusiness }) => {
  const isEmpty = useAuiState(isNewChatView);
  return (
    <ThreadContext.Provider value={{ taskId, forkAvailable, forkingRunId, onForkRun, onResumeBusiness }}>
    <ThreadComponentsContext.Provider value={components}>
      <ThreadPrimitive.Root className="aui-root aui-thread-root bg-background flex h-full min-h-0 flex-col">
        <ThreadPrimitive.Viewport className="relative flex min-h-0 flex-1 flex-col overflow-x-hidden overflow-y-auto scroll-smooth">
          <div className={cn("mx-auto flex w-full max-w-3xl flex-1 flex-col px-4 pt-4", isEmpty && "justify-center")}>
            <div className="mb-14 flex flex-col gap-y-6 empty:hidden">
              <ThreadPrimitive.Messages>{() => <ThreadMessage />}</ThreadPrimitive.Messages>
            </div>
            <ThreadPrimitive.ViewportFooter className={cn("bg-background sticky bottom-0 mt-auto flex flex-col gap-4 pb-4 md:pb-6", !isEmpty && "rounded-t-3xl")}>
              <ThreadPrimitive.ScrollToBottom
                render={<TooltipIconButton tooltip="回到底部" variant="outline" className="absolute -top-12 self-center rounded-full p-3 disabled:invisible" />}
              >
                <ArrowDownIcon />
              </ThreadPrimitive.ScrollToBottom>
              <Composer autoFocus={autoFocus} taskId={taskId} />
            </ThreadPrimitive.ViewportFooter>
          </div>
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
    </ThreadComponentsContext.Provider>
    </ThreadContext.Provider>
  );
};

const Composer: FC<{ autoFocus: boolean; taskId?: number }> = ({ autoFocus, taskId }) => (
  <ComposerPrimitive.Root className="border-border/60 bg-card flex w-full flex-col gap-2 rounded-3xl border p-2 shadow-sm">
    <ComposerPrimitive.Input
      placeholder="输入任务，例如：帮我查找登录相关代码…"
      className="text-foreground placeholder:text-muted-foreground/60 max-h-48 min-h-20 w-full resize-none bg-transparent px-2.5 py-1 text-base leading-6 outline-none"
      rows={2}
      autoFocus={autoFocus}
      enterKeyHint="send"
      aria-label="消息输入"
    />
    <div className="flex flex-wrap items-center justify-between gap-2 px-1">
      <div className="flex min-w-0 items-center gap-1">
        <TaskContextUsage />
        <ComposerControls
          scope={taskId == null ? undefined : { kind: "task", id: taskId }}
          runtimeModelContext
        />
      </div>
      <ComposerAction taskId={taskId ?? null} />
    </div>
  </ComposerPrimitive.Root>
);

const ComposerAction: FC<{ taskId: number | null }> = ({ taskId }) => {
  const aui = useAui();
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const draftLength = useAuiState((state) => state.composer.text.length);
  const isDraftEmpty = useAuiState((state) => state.composer.text.trim().length === 0);
  const canResume = useAuiState((state) => isResumableCancelledRun(state.thread.state as unknown as TransportState));
  const action = deriveComposerAction({ isRunning, isDraftEmpty, canResume });
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const { onResumeBusiness } = useContext(ThreadContext);

  useEffect(() => {
    void frontendLog("DEBUG", "composer_action_derived", "Composer action 状态发生变化", {
      data: { taskId, action, isRunning, isDraftEmpty, canResume, draftLength },
    });
  }, [action, canResume, draftLength, isDraftEmpty, isRunning, taskId]);

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
    return <ComposerPrimitive.Cancel render={<StopButton taskId={taskId} />} />;
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
        setResumeError(error instanceof Error ? error.message : "继续运行失败");
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
  if (isEditing) return <UserEditMessage />;
  return <UserMessageView />;
};

const UserMessageView: FC = () => {
  const messageId = useAuiState((state) => state.message.id);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const canEdit = useAuiState((state) => !isRunning && isEditableLatestRunUserMessage(
    state.thread.state as unknown as TransportState,
    messageId,
  ));

  return (
    <MessagePrimitive.Root data-role="user" className="group flex flex-col items-end px-2">
      <div className="bg-muted text-foreground max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed wrap-break-word">
        <MessagePrimitive.Parts>{({ part }) => part.type === "text" ? <MarkdownText status={part.status} /> : null}</MessagePrimitive.Parts>
      </div>
      <ActionBarPrimitive.Root
        className="mt-1 flex gap-1 opacity-0 transition-opacity pointer-events-none group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100"
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
    </MessagePrimitive.Root>
  );
};

const UserEditMessage: FC = () => {
  const { taskId } = useContext(ThreadContext);
  return (
    <MessagePrimitive.Root data-role="user" className="px-2">
      <ComposerPrimitive.Root className="border-border/60 bg-card flex w-full flex-col gap-2 rounded-3xl border p-2 shadow-sm">
        <ComposerPrimitive.Input
          className="text-foreground placeholder:text-muted-foreground/60 max-h-48 min-h-20 w-full resize-none bg-transparent px-2.5 py-1 text-base leading-6 outline-none"
          rows={2}
          autoFocus
          enterKeyHint="send"
          aria-label="编辑消息"
        />
        <div className="flex flex-wrap items-center justify-between gap-2 px-1">
          <ComposerControls
            scope={taskId == null ? undefined : { kind: "task", id: taskId }}
          />
          <div className="flex items-center gap-1.5">
            <ComposerPrimitive.Cancel render={<TooltipIconButton tooltip="取消编辑" aria-label="取消编辑" />}>
              <XIcon />
            </ComposerPrimitive.Cancel>
            <ComposerPrimitive.Send render={<Button type="button" size="icon" className="size-8 rounded-full" aria-label="重跑" />}>
              <ArrowUpIcon className="size-4" />
            </ComposerPrimitive.Send>
          </div>
        </div>
      </ComposerPrimitive.Root>
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
  const { forkAvailable = false, forkingRunId = null, onForkRun } = useContext(ThreadContext);
  const canFork = isLastRunMessage && runId !== null && forkAvailable && !isRunning;
  const isForking = runId !== null && forkingRunId === runId;

  return (
    <MessagePrimitive.Root data-role="assistant" className="relative -mb-6 pb-6 px-2">
      <div className="text-foreground leading-relaxed wrap-break-word">
        <MessagePrimitive.GroupedParts
          groupBy={assistantMessageGroupBy}
        >
          {({ part, children }) => {
            switch (part.type) {
              case "group-tool-trace": {
                const isToolRunning = part.status.type === "running";
                return (
                  <ToolGroupRoot variant="ghost">
                    <ToolGroupTrigger count={part.indices.length} active={isToolRunning} />
                    <ToolGroupContent>{children}</ToolGroupContent>
                  </ToolGroupRoot>
                );
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
                return <ToolPart {...part} />;
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
      <RunUsageDisplay runId={runId} visible={isLastRunMessage} />
      <ActionBarPrimitive.Root hideWhenRunning className="mt-1 flex gap-1">
        <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip="复制" size="sm" />}>
          <AuiIf condition={(state) => state.message.isCopied}><CheckIcon /></AuiIf>
          <AuiIf condition={(state) => !state.message.isCopied}><CopyIcon /></AuiIf>
        </ActionBarPrimitive.Copy>
        {isLastRunMessage && runId !== null && (
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

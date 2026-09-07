"use client";

import { ArrowDownIcon, ArrowUpIcon, CopyIcon, CheckIcon } from "lucide-react";
import { useContext, createContext, type FC, type ComponentType } from "react";
import {
  AuiIf,
  ActionBarPrimitive,
  ComposerPrimitive,
  ErrorPrimitive,
  groupPartByType,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiState,
  type AssistantState,
} from "@assistant-ui/react";

import { StopButton } from "@/components/assistant/stop-button";
import { ComposerControls } from "@/components/composer/composer-controls";
import { MarkdownText } from "@/components/markdown-text";
import {
  Reasoning,
  ReasoningContent,
  ReasoningRoot,
  ReasoningText,
  ReasoningTrigger,
} from "@/components/assistant-ui/elements/reasoning.aui";
import { ToolPart } from "@/components/assistant-ui/tools/tool-part";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { cn } from "@/lib/utils";

export type ThreadComponents = {
  AssistantMessage?: ComponentType;
  Welcome?: ComponentType;
};

export type ThreadProps = {
  components?: ThreadComponents;
  autoFocus?: boolean;
  taskId?: number;
};

const EMPTY_COMPONENTS: ThreadComponents = {};
const ThreadComponentsContext = createContext<ThreadComponents>(EMPTY_COMPONENTS);

const isNewChatView = (state: AssistantState) => state.thread.messages.length === 0;

export const Thread: FC<ThreadProps> = ({ components = EMPTY_COMPONENTS, autoFocus = true, taskId }) => {
  const isEmpty = useAuiState(isNewChatView);
  return (
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
      <ComposerControls taskId={taskId} />
      <div className="flex items-center gap-1.5">
        <AuiIf condition={(state) => !state.thread.isRunning}>
          <ComposerPrimitive.Send render={<Button type="button" size="icon" className="size-8 rounded-full" aria-label="发送" />}>
            <ArrowUpIcon className="size-4" />
          </ComposerPrimitive.Send>
        </AuiIf>
        <AuiIf condition={(state) => state.thread.isRunning}>
          <ComposerPrimitive.Cancel render={<StopButton taskId={taskId ?? null} />} />
        </AuiIf>
      </div>
    </div>
  </ComposerPrimitive.Root>
);

const ThreadMessage: FC = () => {
  const role = useAuiState((state) => state.message.role);
  const { AssistantMessage = AssistantMessageDefault } = useContext(ThreadComponentsContext);
  return role === "user" ? <UserMessage /> : <AssistantMessage />;
};

const UserMessage: FC = () => (
  <MessagePrimitive.Root data-role="user" className="flex justify-end px-2">
    <div className="bg-muted text-foreground max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed wrap-break-word">
      <MessagePrimitive.Parts>{({ part }) => part.type === "text" ? <MarkdownText status={part.status} /> : null}</MessagePrimitive.Parts>
    </div>
  </MessagePrimitive.Root>
);

const MessageError: FC = () => (
  <MessagePrimitive.Error>
    <ErrorPrimitive.Root className="border-destructive bg-destructive/10 text-destructive mt-2 rounded-md border p-3 text-sm">
      <ErrorPrimitive.Message className="line-clamp-2" />
    </ErrorPrimitive.Root>
  </MessagePrimitive.Error>
);

const AssistantMessageDefault: FC = () => (
  <MessagePrimitive.Root data-role="assistant" className="relative -mb-6 pb-6 px-2">
    <div className="text-foreground leading-relaxed wrap-break-word">
      <MessagePrimitive.GroupedParts
        groupBy={groupPartByType({ reasoning: ["group-reasoning"] })}
      >
        {({ part, children }) => {
          switch (part.type) {
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
    <ActionBarPrimitive.Root hideWhenRunning autohide="not-last" className="mt-1 flex gap-1">
      <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip="复制" size="sm" />}>
        <AuiIf condition={(state) => state.message.isCopied}><CheckIcon /></AuiIf>
        <AuiIf condition={(state) => !state.message.isCopied}><CopyIcon /></AuiIf>
      </ActionBarPrimitive.Copy>
    </ActionBarPrimitive.Root>
  </MessagePrimitive.Root>
);

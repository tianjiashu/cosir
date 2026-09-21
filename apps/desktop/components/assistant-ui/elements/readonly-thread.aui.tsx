"use client";

import { MessagePrimitive, ReadonlyThreadProvider, ThreadPrimitive, useAuiState, type ThreadMessage } from "@assistant-ui/react";
import { useMemo, useRef } from "react";
import { MarkdownText } from "@/components/markdown-text";
import { Reasoning } from "@/components/assistant-ui/elements/reasoning.aui";
import { ToolPart } from "@/components/assistant-ui/tools/tool-part";
import { VirtualizedThreadMessages } from "@/components/assistant-ui/elements/virtualized-thread-messages";

function ReadonlyMessage({ taskId }: { taskId?: number }) {
  const role = useAuiState((state) => state.message.role);
  return (
    <MessagePrimitive.Root
      data-role={role}
      className={role === "user" ? "bg-muted/50 text-foreground ml-auto max-w-[92%] rounded-2xl px-3 py-2" : "text-foreground max-w-none px-1 py-2 leading-relaxed wrap-break-word"}
    >
      <MessagePrimitive.Parts>
        {({ part }) => {
          switch (part.type) {
            case "text":
              return <MarkdownText status={part.status} />;
            case "reasoning":
              return <Reasoning {...part} />;
            case "tool-call":
              return <ToolPart {...part} taskId={taskId} />;
            default:
              return null;
          }
        }}
      </MessagePrimitive.Parts>
    </MessagePrimitive.Root>
  );
}

/** Render transport messages with assistant-ui's readonly runtime provider. */
export function ReadonlyThread({ messages, taskId }: { messages: readonly ThreadMessage[]; taskId?: number }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const messageComponents = useMemo(() => ({ Message: () => <ReadonlyMessage taskId={taskId} /> }), [taskId]);
  return (
    <ReadonlyThreadProvider messages={messages}>
      <ThreadPrimitive.Root className="aui-root flex h-full min-h-0 min-w-0 flex-col bg-background">
      <ThreadPrimitive.Viewport ref={viewportRef} className="flex min-h-0 min-w-0 flex-1 flex-col overflow-x-hidden overflow-y-auto">
          <div className="mx-auto flex min-w-0 w-full max-w-4xl flex-1 flex-col gap-2 px-4 py-4">
            <VirtualizedThreadMessages
              scrollElementRef={viewportRef}
              components={messageComponents}
              rowPaddingBottom="0.5rem"
            />
          </div>
      </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
    </ReadonlyThreadProvider>
  );
}

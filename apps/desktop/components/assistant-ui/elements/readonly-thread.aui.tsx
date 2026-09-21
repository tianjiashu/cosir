"use client";

import { ReadonlyThreadProvider, type ThreadMessage } from "@assistant-ui/react";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";

/** Render the canonical Thread message UI in a read-only runtime scope. */
export function ReadonlyThread({ messages, taskId }: { messages: readonly ThreadMessage[]; taskId?: number }) {
  return (
    <ReadonlyThreadProvider messages={messages}>
      <Thread readonly autoFocus={false} taskId={taskId} />
    </ReadonlyThreadProvider>
  );
}

"use client";

import { ThreadPrimitive, useAuiState } from "@assistant-ui/react";
import { memo, type FC } from "react";

import type { MessageComponents } from "@/components/assistant-ui/elements/virtualized-thread-message-row";
import { queuedMessageIdsKey } from "@/lib/assistant/transport-state-operations";

type QueuedMessagesProps = {
  components: MessageComponents;
};

/**
 * 输入框上方的队列区：渲染尚未并入的排队用户输入，消息流里不再显示它们。
 *
 * 排队条目仍是 assistant-ui 线程里的消息（frame store 把 `queued_messages` 接在已并入消息之后），
 * 这里按 id 在消息作用域里渲染，附件与文件 token 沿用消息行的展示，不必另写一套。队列事实只来自
 * 后端 snapshot；条目的具体外观与操作由 `components` 决定。
 */
export const QueuedMessages: FC<QueuedMessagesProps> = memo(function QueuedMessages({ components }) {
  const idsKey = useAuiState((state) => queuedMessageIdsKey(state.thread.state));
  if (!idsKey) return null;
  const ids = idsKey.split("|");
  return (
    <section aria-label="排队消息" className="flex min-w-0 flex-col gap-1.5">
      <div className="text-muted-foreground flex items-baseline justify-between px-1 text-xs">
        <span>{ids.length} 条排队</span>
        <span>当前运行结束后依次处理</span>
      </div>
      {ids.map((messageId) => (
        <ThreadPrimitive.Unstable_MessageById key={messageId} messageId={messageId} components={components} />
      ))}
    </section>
  );
});

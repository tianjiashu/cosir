import { MessagePrimitive, ThreadPrimitive, useAuiState } from "@assistant-ui/react";
import { memo, type FC } from "react";

import { QueuedMessageActions } from "@/components/conversation/thread/queued-message-actions";
import { UserMessageContent, useHasUserMessageContent } from "@/components/conversation/thread/user-message-content";
import type { MessageComponents } from "@/components/conversation/thread/virtualized-thread-message-row";
import { queueBarMessageIdsKey } from "@/lib/assistant/composer/action-policy";
import { cn } from "@/lib/utils";

type QueuedMessageBarProps = {
  components: MessageComponents;
};

/**
 * 输入框正上方的排队条：⌘回车加入队列、当前 Run 正常结束后才处理的消息，一条一行。Run 被停止或失败时后端不自动
 * 发送它们（规则与理由见后端 `CONTINUE_AFTER_STATUSES`），条目原样留着等用户立即发送、编辑或删除；界面有意不为此
 * 另加说明。插话（默认发送）不在这里，
 * 它们是对话流末尾的用户气泡，操作两处共用（见 `QueuedMessageActions`）。
 *
 * 条目仍是 assistant-ui 线程里的消息（frame store 把 `queued_messages` 与正在提交的命令接在已并入消息之后），
 * 按 id 在消息作用域里渲染，正文与胶囊沿用消息行的展示；哪些在这里只由 `queueBarMessageIdsKey` 判定。
 */
export const QueuedMessageBar: FC<QueuedMessageBarProps> = memo(function QueuedMessageBar({ components }) {
  const idsKey = useAuiState((state) => queueBarMessageIdsKey(state.thread.messages));
  if (!idsKey) return null;
  const ids = idsKey.split("|");
  return (
    <section aria-label="排队消息" className="bg-muted flex min-w-0 flex-col rounded-xl px-1.5 py-1">
      {ids.map((messageId) => (
        <ThreadPrimitive.Unstable_MessageById key={messageId} messageId={messageId} components={components} />
      ))}
    </section>
  );
});

/**
 * 排队条里的一行：正文预览（单行截断）· 操作（见 `QueuedMessageActions`）。条数不另写，行数本身就是。
 * 正在提交的排队发送还没有后端身份：整行淡显。
 */
export const QueuedMessageRow: FC<{ sessionId: number }> = ({ sessionId }) => {
  const submitting = useAuiState((state) => state.message.metadata.custom?.pending === true);
  const hasContent = useHasUserMessageContent();
  return (
    <MessagePrimitive.Root data-role="user" data-queued="next_run" className={cn("flex min-h-9 min-w-0 items-center gap-3 px-2.5", submitting && "opacity-60")}>
      {/* 正文是 Markdown 块，压成行内再单行截断，长消息不撑高排队条。 */}
      <div className="text-foreground line-clamp-1 min-w-0 flex-1 text-sm break-all [&_*]:inline">
        {hasContent ? <UserMessageContent /> : "图片"}
      </div>
      <QueuedMessageActions sessionId={sessionId} />
    </MessagePrimitive.Root>
  );
};

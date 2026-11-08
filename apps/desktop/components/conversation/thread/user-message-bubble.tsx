import { useId, useRef, type ReactNode } from "react";
import { ChevronDownIcon } from "lucide-react";

import { useClipped } from "@/hooks/use-clipped";
import { useDisclosure, userMessageKey } from "@/lib/assistant/turn/disclosure-store";
import { USER_MESSAGE_CLAMP_HEIGHT } from "@/lib/assistant/turn/user-message-clamp";
import { cn } from "@/lib/utils";

/**
 * 用户消息气泡：长消息默认收起到固定高度，底部渐隐，气泡内给「显示全部 / 收起」。
 *
 * 长消息是用户自己刚写的，全文铺开只会把回复推远，所以刚发出的那条也收起。是否截断按渲染后的
 * 高度判断（Markdown、行内胶囊、代码块排出来高低差很多，字数不可靠），放得下时没有渐隐也没有入口。
 * 渐隐用遮罩：收起的气泡是不滚动的小盒子，不踩 `scroll-fade-top` 那条长滚动区的顾虑，深浅主题与
 * 胶囊也一起正确淡出。开合记在 Task 分桶的开合状态里，虚拟列表卸载再挂载不丢；估高读同一个键与
 * 同一组尺寸（`user-message-clamp`）。复制取消息全文，与收起无关。
 */
export function UserMessageBubble({ taskId, messageId, children }: { taskId: number; messageId: string; children: ReactNode }) {
  const [open, setOpen] = useDisclosure(taskId, userMessageKey(messageId), false);
  const contentRef = useRef<HTMLDivElement>(null);
  const clipped = useClipped(contentRef, !open, [messageId]);
  const contentId = useId();
  const collapsed = clipped && !open;
  return (
    <div className="bg-foreground text-background rounded-[22px] px-4 py-3 text-chat leading-relaxed wrap-break-word">
      <div
        ref={contentRef}
        id={contentId}
        data-user-message-collapsed={collapsed || undefined}
        className={cn(!open && "overflow-hidden", collapsed && "mask-b-from-[calc(100%-4.5rem)]")}
        style={open ? undefined : { maxHeight: USER_MESSAGE_CLAMP_HEIGHT }}
      >
        {children}
      </div>
      {clipped && (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={contentId}
          onClick={() => setOpen(!open)}
          className="text-background/70 hover:text-background focus-visible:ring-background/40 mt-1 inline-flex h-6 items-center gap-1 rounded-sm text-sm outline-none focus-visible:ring-2"
        >
          {open ? "收起" : "显示全部"}
          <ChevronDownIcon aria-hidden="true" className={cn("size-4 transition-transform", open && "rotate-180")} />
        </button>
      )}
    </div>
  );
}

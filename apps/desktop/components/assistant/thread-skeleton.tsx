import type { ReactElement } from "react";

import { Skeleton } from "@/components/ui/skeleton";

/**
 * 任务对话首屏的唯一加载占位。
 *
 * 打开任务要先后经过「解析任务身份」（TaskPage）和「拉取首屏快照」（Assistant）两个阶段；
 * 两处共用本骨架，用户看到的是一段连续的加载，而不是文字转圈后再换成骨架。
 * 版式对齐 Thread：居中的消息列与底部输入框，避免就绪时整体跳动。
 */
export function ThreadSkeleton(): ReactElement {
  return (
    <div role="status" aria-label="正在加载任务" data-testid="task-thread-skeleton" className="flex h-full min-h-0 w-full flex-col">
      <div className="mx-auto flex min-h-0 w-full max-w-conversation flex-1 flex-col gap-6 overflow-hidden px-6 pt-6">
        <Skeleton className="h-10 w-2/5 self-end rounded-2xl" />
        <div className="flex flex-col gap-2.5">
          <Skeleton className="h-4 w-11/12" />
          <Skeleton className="h-4 w-4/5" />
          <Skeleton className="h-4 w-3/5" />
        </div>
        <Skeleton className="h-10 w-1/3 self-end rounded-2xl" />
        <div className="flex flex-col gap-2.5">
          <Skeleton className="h-4 w-5/6" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      </div>
      <div className="mx-auto w-full max-w-conversation px-4 pb-4 md:pb-6">
        <Skeleton className="h-28 w-full rounded-3xl" />
      </div>
    </div>
  );
}

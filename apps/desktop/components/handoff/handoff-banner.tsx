import { HandIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { HandoffEntry } from "@/lib/assistant/contract";
import { useHandBack } from "@/lib/handoff/use-hand-back";

/**
 * 资源面板里的交接提示：Agent 要用户做的事，以及「交还」。终端面板与浏览器面板共用，各自按 `surface` 与
 * 资源 id 从 `lib/handoff/store` 取到条目后放在自己的内容上方。
 */
export function HandoffBanner({ taskId, entry, interactive = true }: { taskId: number; entry: HandoffEntry; interactive?: boolean }) {
  const { handBack, returning } = useHandBack(taskId, entry.toolCallId);
  return (
    <div role="alert" data-testid="handoff-banner" className="bg-info/10 text-foreground mx-2 mb-1 flex items-center gap-2 rounded-md px-3 py-1.5 text-xs">
      <HandIcon className="text-info size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate" title={entry.reason}>交给你操作：{entry.reason || "请在这里完成操作"}</span>
      {interactive && (
        <Button type="button" size="xs" disabled={returning} onClick={() => void handBack(entry.toolCallId)}>
          {returning ? "交还中…" : "交还给 Agent"}
        </Button>
      )}
    </div>
  );
}

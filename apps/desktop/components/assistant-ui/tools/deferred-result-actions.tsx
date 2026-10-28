import { useState } from "react";
import { ArrowDownToLineIcon, LoaderCircleIcon, SquareIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { backgroundToolCall, cancelToolCall } from "@/lib/assistant/tool-call-commands";
import { notify } from "@/lib/notify";
import { useDeferredResults } from "@/lib/task-overview/deferred-results-store";

import type { ToolArtifact } from "./types";

/**
 * 延迟结果的卡片操作行，由 `ToolPart` 对所有工具统一挂载，不按工具名分支。
 *
 * - 后台占位（`artifact.deferred` 且仍为 `pending`）：显示「后台执行中」与「取消」；状态取自占位行，
 *   取消后等 snapshot 把占位推到 `cancelled`。
 * - 前台等待（看板里有该调用的前台运行条目）：显示「转到后台」；前台条目只存在于 `deferred_results`。
 *
 * 两类都不是时不渲染。按钮只发命令，请求期间禁用，不做乐观更新。
 */
export function DeferredResultActions({
  taskId,
  toolCallId,
  artifact,
}: {
  taskId: number;
  toolCallId: string;
  artifact: ToolArtifact;
}) {
  const entries = useDeferredResults(taskId);
  const [busy, setBusy] = useState(false);
  const background = artifact.deferred === true && artifact.backendStatus === "pending";
  const foreground = !background && entries.some(
    (entry) => entry.toolCallId === toolCallId && entry.status === "running" && entry.mode === "foreground",
  );
  if (!background && !foreground) return null;

  const run = async (action: () => Promise<string | null>) => {
    setBusy(true);
    try {
      const failure = await action();
      if (failure !== null) notify.error(failure);
    } finally {
      setBusy(false);
    }
  };
  const cancel = () => run(async () => {
    const result = await cancelToolCall(taskId, toolCallId);
    return result.kind === "failed" ? result.message : null;
  });
  const detach = () => run(async () => {
    const result = await backgroundToolCall(taskId, toolCallId);
    return result.kind === "failed" ? result.message : null;
  });

  return (
    <div className="text-muted-foreground mt-1 flex items-center gap-2 text-xs" data-deferred-actions={toolCallId}>
      {background ? (
        <>
          <span className="inline-flex items-center gap-1.5">
            <LoaderCircleIcon className="size-3.5 animate-spin" aria-hidden="true" />
            后台执行中
          </span>
          <Button variant="ghost" size="xs" disabled={busy} onClick={cancel}>
            <SquareIcon aria-hidden="true" />
            取消
          </Button>
        </>
      ) : (
        <Button variant="ghost" size="xs" disabled={busy} onClick={detach}>
          <ArrowDownToLineIcon aria-hidden="true" />
          转到后台
        </Button>
      )}
    </div>
  );
}

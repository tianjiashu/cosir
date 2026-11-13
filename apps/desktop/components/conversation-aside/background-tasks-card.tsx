import { useEffect, useState } from "react";
import { SquareIcon } from "lucide-react";

import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { commandPending, stopTarget, useCommandRequests } from "@/lib/assistant/command-request-store";
import { cancelToolCall } from "@/lib/assistant/session-commands";
import { countRunning, useBackgroundTasks } from "@/lib/background-tasks/background-tasks-store";
import { useConversationAsideStore } from "@/lib/conversation-aside/conversation-aside-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

import { BackgroundTaskRow } from "./background-task-row";

/**
 * 「后台任务」卡（会话侧栏）。条目只读 snapshot `background_tasks`（后端从持久化占位与活登记投影）。
 * 这个 Session 有过后台工作（含已结束的）才算有内容，所以一个 Session 里它只出现一次、不来回闪；没有时
 * 卡片不渲染。折叠、拖拽由侧栏的卡片外壳负责，这里只给内容判定、摘要与正文。
 */

export function useBackgroundTasksHasContent(sessionId: number | null): boolean {
  return useBackgroundTasks(sessionId).length > 0;
}

/** 折叠时标题行右侧的一句话：有运行中的报运行数，其次是收尾中，否则报已结束数。 */
export function BackgroundTasksSummary({ sessionId }: { sessionId: number }) {
  const entries = useBackgroundTasks(sessionId);
  const running = countRunning(entries);
  if (running > 0) return <>{running} 个运行中</>;
  if (entries.some((entry) => entry.status === "settling")) return <>收尾中</>;
  return <>{entries.length} 已结束</>;
}

/**
 * 标题行的「全部停止」：对每条运行中的条目发一次单停（`cancelToolCall`），与逐行停止是同一个命令、同一个
 * 「停止中」槽位，后端没有批量命令。用户停止 Session 不动后台工作，这里是一次停掉它们的入口；每条停止的
 * 回执照常交给 Agent（不开新 Run），它因此知道哪些工作被停了。只停点下那一刻看到的条目。
 */
export function BackgroundTasksActions({ sessionId }: { sessionId: number }) {
  const running = useBackgroundTasks(sessionId).filter((entry) => entry.status === "running");
  const requests = useCommandRequests((state) => state.sessions[sessionId]);
  if (running.length === 0) return null;
  const busy = running.every((entry) => commandPending(requests?.[stopTarget(entry.toolCallId)]));

  const stopAll = async () => {
    void frontendLog("INFO", "background_stop_all_requested", "用户停止全部后台任务", { data: { sessionId, count: running.length } });
    const failures = await Promise.all(running.map((entry) => cancelToolCall(sessionId, entry.toolCallId)));
    const failure = failures.find((item) => item !== null);
    if (failure) notify.error(failure);
  };

  return (
    <TooltipIconButton tooltip={busy ? "正在停止" : "全部停止"} className="size-6 shrink-0" disabled={busy} onClick={() => void stopAll()}>
      <SquareIcon aria-hidden="true" />
    </TooltipIconButton>
  );
}

/**
 * 进行中（运行、收尾）的条目逐行列出，已结束的收成一行「N 已结束」，点开展开。耗时由各行自己的计时叶子
 * 刷新（只有运行中的行起计时器），列表本身不随时钟重渲染。
 */
export function BackgroundTasksBody({ sessionId }: { sessionId: number }) {
  const entries = useBackgroundTasks(sessionId);
  const [showEnded, setShowEnded] = useState(false);
  const active = entries.filter((entry) => entry.status === "running" || entry.status === "settling");
  const ended = entries.filter((entry) => entry.status === "completed" || entry.status === "cancelled");
  // 定位目标在折叠的「已结束」里时先展开列表，行挂载后才接得住定位。
  const revealEnded = useConversationAsideStore((state) => state.revealCall?.sessionId === sessionId && ended.some((entry) => entry.toolCallId === state.revealCall?.callId));
  useEffect(() => { if (revealEnded) setShowEnded(true); }, [revealEnded]);

  return (
    <>
      <ul className="flex flex-col gap-0.5">
        {active.map((entry) => (
          <BackgroundTaskRow key={entry.toolCallId} sessionId={sessionId} entry={entry} />
        ))}
      </ul>
      {ended.length > 0 && (
        <div className="mt-1">
          <button
            type="button"
            className="group/disclosure text-muted-foreground hover:text-foreground flex items-center gap-1.5 rounded-sm px-1 py-1 text-xs outline-none"
            aria-expanded={showEnded}
            onClick={() => setShowEnded((value) => !value)}
          >
            {ended.length} 已结束
            <DisclosureChevron open={showEnded} reveal />
          </button>
          {showEnded && (
            <ul className="flex flex-col gap-0.5">
              {ended.map((entry) => (
                <BackgroundTaskRow key={entry.toolCallId} sessionId={sessionId} entry={entry} />
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}

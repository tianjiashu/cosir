import { useEffect, useState } from "react";

import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { countRunning, useBackgroundTasks } from "@/lib/background-tasks/background-tasks-store";
import { useTaskRailStore } from "@/lib/task-rail/task-rail-store";

import { BackgroundTaskRow } from "./background-task-row";

/**
 * 「后台任务」卡（会话侧栏）。条目只读 snapshot `background_tasks`（后端从持久化占位与活登记投影）。
 * 卡片始终存在，折叠时只剩摘要；折叠、拖拽由侧栏的卡片外壳负责，这里只给摘要与正文。
 */

/** 折叠时标题行右侧的一句话：有运行中的报运行数，其次是收尾中，再次报已结束数，都没有则为「无」。 */
export function BackgroundTasksSummary({ taskId }: { taskId: number }) {
  const entries = useBackgroundTasks(taskId);
  const running = countRunning(entries);
  if (running > 0) return <>{running} 个运行中</>;
  if (entries.some((entry) => entry.status === "settling")) return <>收尾中</>;
  return <>{entries.length > 0 ? `${entries.length} 已结束` : "无"}</>;
}

/**
 * 进行中（运行、收尾）的条目逐行列出，已结束的收成一行「N 已结束」，点开展开；有运行中的条目时每秒刷新
 * 耗时，否则不起计时器（收尾中的条目不计时）。
 */
export function BackgroundTasksBody({ taskId }: { taskId: number }) {
  const entries = useBackgroundTasks(taskId);
  const [showEnded, setShowEnded] = useState(false);
  const active = entries.filter((entry) => entry.status === "running" || entry.status === "settling");
  const ended = entries.filter((entry) => entry.status === "completed" || entry.status === "cancelled");
  const now = useNow(countRunning(entries) > 0);
  // 定位目标在折叠的「已结束」里时先展开列表，行挂载后才接得住定位。
  const revealEnded = useTaskRailStore((state) => state.revealCall?.taskId === taskId && ended.some((entry) => entry.toolCallId === state.revealCall?.callId));
  useEffect(() => { if (revealEnded) setShowEnded(true); }, [revealEnded]);
  if (entries.length === 0) return <p className="text-muted-foreground px-1 py-1 text-xs">没有后台任务</p>;

  return (
    <>
      <ul className="flex flex-col gap-0.5">
        {active.map((entry) => (
          <BackgroundTaskRow key={entry.toolCallId} taskId={taskId} entry={entry} now={now} />
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
                <BackgroundTaskRow key={entry.toolCallId} taskId={taskId} entry={entry} now={now} />
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}

function useNow(ticking: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [ticking]);
  return now;
}

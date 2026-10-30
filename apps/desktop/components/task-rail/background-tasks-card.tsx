import { useEffect, useState } from "react";
import { ChevronRightIcon } from "lucide-react";

import { countRunning, useBackgroundTasks } from "@/lib/background-tasks/store";

import { BackgroundTaskRow } from "./background-task-row";

/**
 * 「后台任务」卡（会话侧栏）。条目只读 snapshot `background_tasks`（后端进程内事实，重启后清空）。
 * 卡片始终存在，折叠时只剩摘要；折叠、拖拽由侧栏的卡片外壳负责，这里只给摘要与正文。
 */

/** 折叠时标题行右侧的一句话：有运行中的报运行数，否则报已完成数，都没有则为「无」。 */
export function BackgroundTasksSummary({ taskId }: { taskId: number }) {
  const entries = useBackgroundTasks(taskId);
  const running = countRunning(entries);
  if (running > 0) return <>{running} 个运行中</>;
  return <>{entries.length > 0 ? `${entries.length} 已完成` : "无"}</>;
}

/** 运行中的条目逐行列出，已结束的收成一行「N 已完成」，点开展开；运行中的条目每秒刷新耗时，否则不起计时器。 */
export function BackgroundTasksBody({ taskId }: { taskId: number }) {
  const entries = useBackgroundTasks(taskId);
  const [showEnded, setShowEnded] = useState(false);
  const running = entries.filter((entry) => entry.status === "running");
  const ended = entries.filter((entry) => entry.status !== "running");
  const now = useNow(running.length > 0);
  if (entries.length === 0) return <p className="text-muted-foreground px-1 py-1 text-xs">没有后台任务</p>;

  return (
    <>
      <ul className="flex flex-col gap-0.5">
        {running.map((entry) => (
          <BackgroundTaskRow key={entry.toolCallId} taskId={taskId} entry={entry} now={now} />
        ))}
      </ul>
      {ended.length > 0 && (
        <div className="mt-1">
          <button
            type="button"
            className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 rounded-sm px-1 py-1 text-xs outline-none"
            aria-expanded={showEnded}
            onClick={() => setShowEnded((value) => !value)}
          >
            <ChevronRightIcon className={showEnded ? "size-3.5 rotate-90 transition-transform" : "size-3.5 transition-transform"} aria-hidden="true" />
            {ended.length} 已完成
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

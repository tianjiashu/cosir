import { useEffect, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { useDeferredResults } from "@/lib/task-overview/deferred-results-store";
import { readDeferredCardCollapsed, writeDeferredCardCollapsed } from "@/lib/task-overview/overview-preferences";

import { DeferredResultRow } from "./deferred-result-row";

/**
 * 「后台任务」卡：运行中的条目逐行列出，已结束的收成一行「N 已完成」，点开展开。
 *
 * 条目只读 snapshot `deferred_results`（后端进程内事实，重启后清空）；没有任何条目时不渲染。
 * 运行中的条目每秒刷新一次耗时，没有运行中条目时不起计时器。
 */
export function DeferredResultsCard({ taskId }: { taskId: number }) {
  const entries = useDeferredResults(taskId);
  const [collapsed, setCollapsed] = useState(readDeferredCardCollapsed);
  const [showEnded, setShowEnded] = useState(false);
  const running = entries.filter((entry) => entry.status === "running");
  const ended = entries.filter((entry) => entry.status !== "running");
  const now = useNow(running.length > 0);
  if (entries.length === 0) return null;

  const toggle = (open: boolean) => {
    setCollapsed(!open);
    writeDeferredCardCollapsed(!open);
  };

  return (
    <section className="bg-card rounded-xl border p-3 text-sm shadow-xs" aria-label="后台任务">
      <Collapsible open={!collapsed} onOpenChange={toggle}>
        <CollapsibleTrigger className="flex w-full items-center gap-1.5 font-medium outline-none">
          后台任务
          {running.length > 0 && <span className="text-muted-foreground text-xs tabular-nums">{running.length}</span>}
          <ChevronDownIcon className={collapsed ? "size-3.5 -rotate-90 transition-transform" : "size-3.5 transition-transform"} aria-hidden="true" />
        </CollapsibleTrigger>
        <CollapsibleContent>
          <ul className="mt-2 flex flex-col gap-0.5">
            {running.map((entry) => (
              <DeferredResultRow key={entry.toolCallId} taskId={taskId} entry={entry} now={now} />
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
                    <DeferredResultRow key={entry.toolCallId} taskId={taskId} entry={entry} now={now} />
                  ))}
                </ul>
              )}
            </div>
          )}
        </CollapsibleContent>
      </Collapsible>
    </section>
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

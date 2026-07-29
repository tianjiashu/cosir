/**
 * 日志页面。
 *
 * 通过后端日志查询 API 渲染最近日志或指定 trace 链路日志。
 *
 * @module pages/logs/LogsPage
 */

import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, RefreshCw, Search } from "lucide-react";
import type { LogLevel, LogQueryResponse } from "@shared/logs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { fetchLogsByTrace, fetchRecentLogs } from "@/services/logs";
import { logError, logInfo } from "@/lib/logger";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";
import { useTaskStore } from "@/stores/taskStore";

/** 日志页面属性。 */
interface LogsPageProps {
  /** 返回主会话页面。 */
  onBack: () => void;
}

/**
 * LogsPage 页面组件。
 *
 * @param props - 页面属性。
 * @returns 日志查看页面。
 */
export function LogsPage({ onBack }: LogsPageProps) {
  const activeTaskId = useTaskStore((state) => state.activeTaskId);
  const activeStreamTrace = useConversationTraceStore((state) =>
    activeTaskId ? state.streamTraceByTaskId[activeTaskId] ?? null : null,
  );
  const activeConversationTrace = useConversationTraceStore((state) =>
    activeTaskId ? state.latestTraceByTaskId[activeTaskId] ?? null : null,
  );
  const defaultTrace = activeStreamTrace ?? activeConversationTrace;
  const [logs, setLogs] = useState<LogQueryResponse>({ entries: [], text: "" });
  const [traceId, setTraceId] = useState(defaultTrace?.traceId ?? "");
  const [autoLoadedTraceId, setAutoLoadedTraceId] = useState<string | null>(defaultTrace?.traceId ?? null);
  const [userEditedTraceId, setUserEditedTraceId] = useState(false);
  const [level, setLevel] = useState<LogLevel | "">("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * 刷新后端日志。
   *
   * @sideeffect 调用后端日志查询 API，并更新页面状态。
   */
  const refreshLogs = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const trimmedTraceId = traceId.trim();
      const request = { level: level || undefined, limit: 200 };
      const result = trimmedTraceId
        ? await fetchLogsByTrace({ ...request, trace_id: trimmedTraceId })
        : await fetchRecentLogs(request);
      setLogs(result);
      logInfo("日志页面刷新完成", {
        module: "LogsPage",
        entry_count: result.entries.length,
        mode: trimmedTraceId ? "trace" : "recent",
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      logError("日志页面刷新失败", err, { module: "LogsPage" });
    } finally {
      setIsLoading(false);
    }
  }, [level, traceId]);

  useEffect(() => {
    void refreshLogs();
    // 首次进入日志页时加载当前对话 trace 日志；没有 trace 时退回最近日志。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!userEditedTraceId && defaultTrace?.traceId && traceId !== defaultTrace.traceId) {
      setTraceId(defaultTrace.traceId);
    }
  }, [defaultTrace?.traceId, traceId, userEditedTraceId]);

  useEffect(() => {
    if (userEditedTraceId || !defaultTrace?.traceId || autoLoadedTraceId === defaultTrace.traceId) {
      return;
    }
    setAutoLoadedTraceId(defaultTrace.traceId);
    setIsLoading(true);
    setError(null);
    void fetchLogsByTrace({ trace_id: defaultTrace.traceId, level: level || undefined, limit: 200 })
      .then((result) => {
        setLogs(result);
        logInfo("日志页面自动加载当前对话 trace 完成", {
          module: "LogsPage",
          entry_count: result.entries.length,
        });
      })
      .catch((err) => {
        const message = err instanceof Error ? err.message : String(err);
        setError(message);
        logError("日志页面自动加载当前对话 trace 失败", err, { module: "LogsPage" });
      })
      .finally(() => {
        setIsLoading(false);
      });
  }, [autoLoadedTraceId, defaultTrace?.traceId, level, userEditedTraceId]);

  return (
    <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-background">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border px-4">
        <div className="flex min-w-0 items-center gap-2">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  onClick={onBack}
                  aria-label="返回会话"
                >
                  <ArrowLeft className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">返回会话</TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold">日志</h1>
            <p className="truncate text-xs text-muted-foreground">
              {traceId.trim() ? "当前对话 trace 日志" : "最近后端日志"}
            </p>
          </div>
        </div>
        <div className="flex min-w-0 items-center gap-2">
          <Input
            value={traceId}
            onChange={(event) => {
              setUserEditedTraceId(true);
              setTraceId(event.target.value);
            }}
            placeholder="trace_id"
            className="h-8 w-72 font-mono text-xs"
            aria-label="trace_id"
          />
          <select
            value={level}
            onChange={(event) => setLevel(event.target.value as LogLevel | "")}
            className="h-8 rounded-md border border-input bg-background px-2 text-xs"
            aria-label="日志级别"
          >
            <option value="">全部</option>
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARN</option>
            <option value="ERROR">ERROR</option>
            <option value="CRITICAL">CRITICAL</option>
          </select>
          <Button variant="outline" size="sm" className="gap-1" onClick={refreshLogs} disabled={isLoading}>
            {traceId.trim() ? (
              <Search className="h-3.5 w-3.5" />
            ) : (
              <RefreshCw className={isLoading ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
            )}
            {traceId.trim() ? "查询" : "刷新"}
          </Button>
        </div>
      </div>

      {error ? (
        <div className="border-b border-destructive/20 bg-destructive/5 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      <ScrollArea className="flex-1">
        <div className="space-y-4 p-4">
          {logs.entries.length === 0 && !isLoading ? (
            <div className="rounded-md border border-dashed border-border p-6 text-sm text-muted-foreground">
              暂无日志内容
            </div>
          ) : null}
          <section className="overflow-hidden rounded-md border border-border bg-card">
            <pre className="max-h-[600px] overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-xs leading-5 text-foreground">
              {logs.text || "(empty)"}
            </pre>
          </section>
          {logs.entries.length > 0 ? (
            <section className="overflow-hidden rounded-md border border-border bg-card">
              <div className="divide-y divide-border">
                {logs.entries.map((entry, index) => (
                  <article
                    key={`${entry.ts}-${entry.event}-${index}`}
                    className="space-y-2 p-3 font-mono text-xs leading-5 text-foreground"
                  >
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <span className="text-muted-foreground">{entry.ts}</span>
                      <span>{entry.level}</span>
                      <span>{entry.event}</span>
                    </div>
                    {entry.msg ? <p className="whitespace-pre-wrap break-words">{entry.msg}</p> : null}
                    {entry.error?.stack ? (
                      <pre className="whitespace-pre-wrap break-words text-destructive">{entry.error.stack}</pre>
                    ) : null}
                  </article>
                ))}
              </div>
            </section>
          ) : null}
        </div>
      </ScrollArea>
    </main>
  );
}

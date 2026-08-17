/**
 * 日志页面。
 *
 * 通过后端结构化日志查询 API 渲染最近日志或指定 trace 链路日志，采用
 * 结构化卡片 + 虚拟滚动。筛选条件（trace_id / 级别 / 关键词 / 时间）全部
 * 交由后端组合筛选，前端不再做当页客户端过滤；级别分布由后端 level_counts 提供。
 *
 * @module pages/logs/LogsPage
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import type { LogLevel, LogQueryResponse } from "@shared/logs";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { VirtualList } from "@/lib/virtual/VirtualList";
import { fetchLogsByTrace, fetchRecentLogs } from "@/services/logs";
import { logError, logInfo } from "@/lib/logger";
import { LogEntryCard } from "@/components/logs/LogEntryCard";
import { LogStatsBar } from "@/components/logs/LogStatsBar";
import { LogFilterBar } from "@/components/logs/LogFilterBar";

/** 每页拉取的日志条数。 */
const PAGE_SIZE = 50;

/** 日志页面属性。 */
interface LogsPageProps {
  /** 返回主会话页面。 */
  onBack: () => void;
}

/**
 * 生成 RFC3339（带 Z）的过去 N 分钟时间点。
 *
 * @param minutes - 往前推的分钟数。
 * @returns 对应 UTC 时间的 ISO 字符串。
 */
function minutesAgoRfc3339(minutes: number): string {
  return new Date(Date.now() - minutes * 60 * 1000).toISOString();
}

/**
 * LogsPage 页面组件。
 *
 * 负责工具栏（返回/trace_id/级别/关键词/时间快捷/查询刷新）、级别计数栏、
 * 分页与虚拟滚动列表渲染，以及加载/错误/空态展示。
 *
 * @param props - 页面属性。
 * @returns 日志查看页面。
 */
export function LogsPage({ onBack }: LogsPageProps) {
  const [logs, setLogs] = useState<LogQueryResponse>({
    entries: [],
    text: "",
    total: 0,
    has_more: false,
    level_counts: {},
  });
  const [traceId, setTraceId] = useState("");
  const [level, setLevel] = useState<LogLevel | "">("");
  const [keyword, setKeyword] = useState("");
  const [startTime, setStartTime] = useState<string | undefined>(undefined);
  const [offset, setOffset] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);

  /**
   * 刷新后端日志（带分页 offset 与当前筛选条件）。
   *
   * 接受可选的 level / startTime 覆盖值：调用方若想在不等待 state 提交的情况下立即
   * 以新筛选条件刷新，可传入覆盖值；缺省时回退到当前 state。关键词始终读取当前 state
   * 的 keyword（非覆盖），随每次查询提交。
   *
   * @param nextOffset - 分页起点（跳过的记录数），默认 0 表示从首页开始。
   * @param overrides - 可选的筛选覆盖：level 与 start_time。
   * @sideeffect 调用后端日志查询 API，并更新页面状态。
   */
  const refreshLogs = useCallback(
    async (nextOffset = 0, overrides?: { level?: LogLevel | ""; startTime?: string | undefined }) => {
      setIsLoading(true);
      setError(null);
      try {
        const trimmedTraceId = traceId.trim();
        const effectiveLevel = overrides?.level ?? level;
        const effectiveStartTime = overrides?.startTime ?? startTime;
        const effectiveKeyword = keyword.trim();
        const request = {
          level: effectiveLevel || undefined,
          keyword: effectiveKeyword || undefined,
          limit: PAGE_SIZE,
          offset: nextOffset,
          start_time: effectiveStartTime,
        };
        const result = trimmedTraceId
          ? await fetchLogsByTrace({ ...request, trace_id: trimmedTraceId })
          : await fetchRecentLogs(request);
        setLogs(result);
        setOffset(nextOffset);
        logInfo("日志页面刷新完成", {
          module: "LogsPage",
          entry_count: result.entries.length,
          total: result.total,
          mode: trimmedTraceId ? "trace" : "recent",
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setError(message);
        logError("日志页面刷新失败", err, { module: "LogsPage" });
      } finally {
        setIsLoading(false);
      }
    },
    [level, traceId, startTime, keyword],
  );

  useEffect(() => {
    void refreshLogs();
    // 首次进入日志页加载最近日志（第一页）；traceId 留空，由用户手动输入查询。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * 点击级别计数 Badge 切换筛选。
   *
   * 传入空串表示「全部」（清除级别筛选）；传入具体级别则选中该级别。
   * 切换后重置分页并刷新。
   *
   * @param nextLevel - 目标级别或空串（全部）。
   * @sideeffect 更新 level 状态并触发后端刷新。
   */
  const handleSelectLevel = useCallback(
    (nextLevel: LogLevel | "") => {
      setLevel(nextLevel);
      setOffset(0);
      void refreshLogs(0, { level: nextLevel });
    },
    [refreshLogs],
  );

  /**
   * 设置时间快捷筛选并刷新。
   *
   * @param minutes - 往前推的分钟数。
   * @sideeffect 更新 startTime 并触发后端刷新。
   */
  const handleTimePreset = useCallback(
    (minutes: number) => {
      const next = minutesAgoRfc3339(minutes);
      setStartTime(next);
      setOffset(0);
      void refreshLogs(0, { startTime: next });
    },
    [refreshLogs],
  );

  return (
    <main className="flex h-full w-full min-w-0 flex-col overflow-hidden bg-background">
      {/* 顶部工具栏 */}
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
        <LogFilterBar
          traceId={traceId}
          keyword={keyword}
          isLoading={isLoading}
          onTraceIdChange={setTraceId}
          onKeywordChange={setKeyword}
          onTimePreset={handleTimePreset}
          onSearch={() => void refreshLogs(0)}
        />
      </div>

      {/* 级别计数栏（消费后端全量级别分布，忽略 level 过滤、含其余过滤条件） */}
      <LogStatsBar
        levelCounts={logs.level_counts}
        total={logs.total}
        activeLevel={level}
        onSelectLevel={handleSelectLevel}
      />

      {/* 分页统计栏 */}
      <div className="flex h-9 shrink-0 items-center justify-between border-b border-border px-4 text-xs text-muted-foreground">
        <span>
          共 {logs.total} 条
          {logs.total > 0
            ? ` · 第 ${Math.floor(offset / PAGE_SIZE) + 1} / ${Math.ceil(logs.total / PAGE_SIZE)} 页`
            : ""}
        </span>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="h-6 px-2"
            onClick={() => void refreshLogs(Math.max(0, offset - PAGE_SIZE))}
            disabled={isLoading || offset === 0}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-6 px-2"
            onClick={() => void refreshLogs(offset + PAGE_SIZE)}
            disabled={isLoading || !logs.has_more}
          >
            下一页
          </Button>
        </div>
      </div>

      {/* 错误条 */}
      {error ? (
        <div className="border-b border-destructive/20 bg-destructive/5 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      {/* 虚拟滚动列表容器（复用项目通用 VirtualList 原语，动态高度测量 + 空态） */}
      <div className="min-h-0 flex-1 p-4">
        <VirtualList
          items={logs.entries}
          getKey={(entry, index) => entry.trace_id || `${entry.ts}::${index}`}
          renderItem={(entry) => (
            <div className="pb-2">
              <LogEntryCard entry={entry} />
            </div>
          )}
          estimateSize={76}
          scrollContainerRef={scrollRef}
          className="h-full"
          emptyState={
            <div className="flex h-full items-center justify-center rounded-md border border-dashed border-border p-6 text-sm text-muted-foreground">
              {logs.total === 0 ? "暂无日志内容" : "当前筛选条件下无匹配日志"}
            </div>
          }
        />
      </div>
    </main>
  );
}

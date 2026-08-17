/**
 * 单条日志卡片。
 *
 * 以结构化 `LogEntryResponse` 渲染一条日志：左侧级别色标、常驻摘要行、
 * 可折叠元数据（trace_id/caller/logger）、可折叠 data 面板、error 区与截断提示。
 *
 * @module components/logs/LogEntryCard
 */

import { useState } from "react";
import { ChevronDown } from "lucide-react";
import type { LogEntryResponse } from "@shared/logs";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Caption } from "@/components/ui/tokens";
import { LogDataViewer } from "./LogDataViewer";

/** 级别到左侧色标与徽标配色的映射。 */
const LEVEL_STYLES: Record<string, { border: string; dot: string; badge: BadgeProps["variant"] }> = {
  ERROR: { border: "border-l-red-500", dot: "bg-red-500", badge: "destructive" },
  WARNING: { border: "border-l-amber-500", dot: "bg-amber-500", badge: "warning" },
  INFO: { border: "border-l-sky-500", dot: "bg-sky-500", badge: "secondary" },
  DEBUG: { border: "border-l-slate-400", dot: "bg-slate-400", badge: "outline" },
  CRITICAL: { border: "border-l-red-700", dot: "bg-red-700", badge: "destructive" },
};

/**
 * 取默认级别样式（未知级别兜底为 DEBUG 灰）。
 *
 * @param level - 日志级别字符串。
 * @returns 对应的边框/圆点/徽标样式组合。
 */
function levelStyle(level: string) {
  return LEVEL_STYLES[level] ?? LEVEL_STYLES.DEBUG;
}

/**
 * 计算相对时间描述（x分钟前/小时前/天前）。
 *
 * @param ts - RFC3339 时间戳。
 * @returns 人类可读的相对时间；解析失败时回退为原始字符串。
 */
function relativeTime(ts: string): string {
  const then = new Date(ts).getTime();
  if (Number.isNaN(then)) return ts;
  const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (diffSec < 60) return `${diffSec}秒前`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}分钟前`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}小时前`;
  const diffDay = Math.floor(diffHour / 24);
  return `${diffDay}天前`;
}

/**
 * 单条日志卡片组件属性。
 */
interface LogEntryCardProps {
  /** 结构化日志条目。 */
  entry: LogEntryResponse;
}

/**
 * 单条日志卡片组件。
 *
 * 摘要行常驻显示相对时间、级别徽标、event 与 msg 摘要；元数据与 data 默认折叠，
 * 点击展开；若含 error 则渲染类型与消息并以红色高亮，stack 可展开；
 * truncated 为 true 时底部提示内容已截断。
 *
 * @param props - 组件属性。
 * @returns 单条日志的结构化卡片。
 */
export function LogEntryCard({ entry }: LogEntryCardProps) {
  const [metaOpen, setMetaOpen] = useState(false);
  const [dataOpen, setDataOpen] = useState(false);
  const [stackOpen, setStackOpen] = useState(false);

  const style = levelStyle(entry.level);
  const hasData = entry.data && Object.keys(entry.data).length > 0;
  const hasError = entry.error && (entry.error.type || entry.error.message || entry.error.stack);

  return (
    <article
      className={`rounded-md border border-l-4 border-border bg-card ${style.border}`}
    >
      {/* 摘要行：相对时间 + 级别徽标 + event — msg */}
      <div className="flex items-start gap-2 px-3 py-2">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className={Caption.mono + " shrink-0 text-muted-foreground"}>
                {relativeTime(entry.ts)}
              </span>
            </TooltipTrigger>
            <TooltipContent side="top">{entry.ts}</TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <Badge variant={style.badge} className="shrink-0">
          <span className={`mr-1 inline-block h-1.5 w-1.5 rounded-full ${style.dot}`} />
          {entry.level}
        </Badge>

        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-foreground">
            <span className="font-medium">{entry.event}</span>
            {entry.msg ? <span className="text-muted-foreground"> — {entry.msg}</span> : null}
          </p>
        </div>
      </div>

      {/* 元数据折叠区（复用项目 Collapsible 原语，自带 data-state/aria-controls 无障碍） */}
      <Collapsible open={metaOpen} onOpenChange={setMetaOpen} className="border-t border-border">
        <CollapsibleTrigger asChild>
          <button
            type="button"
            className="flex w-full items-center gap-1 px-3 py-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ChevronDown
              className={`h-3 w-3 transition-transform ${metaOpen ? "rotate-180" : ""}`}
            />
            {metaOpen ? "收起元数据" : "展开元数据"}
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent className="space-y-0.5 px-3 pb-2">
          <div className={Caption.mono + " text-muted-foreground"}>
            <span className="text-violet-600">trace_id:</span> {entry.trace_id || "—"}
          </div>
          <div className={Caption.mono + " text-muted-foreground"}>
            <span className="text-violet-600">caller:</span> {entry.caller || "—"}
          </div>
          <div className={Caption.mono + " text-muted-foreground"}>
            <span className="text-violet-600">logger:</span> {entry.logger || "—"}
          </div>
        </CollapsibleContent>
      </Collapsible>

      {/* data 折叠面板（仅有 data 时渲染折叠入口） */}
      {hasData ? (
        <Collapsible open={dataOpen} onOpenChange={setDataOpen} className="border-t border-border">
          <CollapsibleTrigger asChild>
            <button
              type="button"
              className="flex w-full items-center gap-1 px-3 py-2 text-xs text-muted-foreground hover:text-foreground"
            >
              <ChevronDown
                className={`h-3 w-3 transition-transform ${dataOpen ? "rotate-180" : ""}`}
              />
              data（{Object.keys(entry.data ?? {}).length} 字段）
            </button>
          </CollapsibleTrigger>
          <CollapsibleContent className="px-3 pb-2">
            <div className="mt-1 rounded bg-muted/40 p-2">
              <LogDataViewer data={entry.data as Record<string, unknown>} />
            </div>
          </CollapsibleContent>
        </Collapsible>
      ) : null}

      {/* error 区 */}
      {hasError ? (
        <div className="border-t border-destructive/30 bg-destructive/5 px-3 py-2 text-xs">
          <div className="flex items-center gap-1 font-medium text-destructive">
            <span>⚠ {entry.error?.type || "ERROR"}:</span>
            <span>{entry.error?.message || ""}</span>
          </div>
          {entry.error?.stack ? (
            <Collapsible
              open={stackOpen}
              onOpenChange={setStackOpen}
              className="mt-1"
            >
              <CollapsibleTrigger asChild>
                <button
                  type="button"
                  className="flex items-center gap-1 text-destructive/80 hover:text-destructive"
                >
                  <ChevronDown
                    className={`h-3 w-3 transition-transform ${stackOpen ? "rotate-180" : ""}`}
                  />
                  {stackOpen ? "收起堆栈" : "展开堆栈"}
                </button>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-destructive/10 p-2 font-mono text-[11px] text-destructive">
                  {entry.error.stack}
                </pre>
              </CollapsibleContent>
            </Collapsible>
          ) : null}
        </div>
      ) : null}

      {/* 截断提示 */}
      {entry.truncated ? (
        <div className="border-t border-border px-3 py-1 text-[10px] text-muted-foreground">
          内容已截断
        </div>
      ) : null}
    </article>
  );
}
